"""Bounded public-first reads for funding qualification, never execution."""
import asyncio
import logging
import re
import time

from chain_common.rpc import RpcError
from chain_common.public_rpc import defer_429_alerts, report_funding_unavailable


class FundingRpc:
    METHODS = {'getTransaction', 'getAccountInfo', 'getSignaturesForAddress', 'getBlockTime'}

    def __init__(self, public, alchemy=None, *, clock=time.monotonic, prefer_alchemy=False):
        self.public, self.alchemy, self.clock = public, alchemy, clock
        self.prefer_alchemy=prefer_alchemy
        self.slots = {"public": asyncio.Semaphore(1), "alchemy": asyncio.Semaphore(4)}
        self.generation = {"public": 0, "alchemy": 0}
        self.cooldown = {}
        self.failures = {}
        self.counts = dict(calls=0, public_attempts=0, alchemy_attempts=0,
                           recovered=0, unavailable=0)
        self.last_failure = {}
        self.failure_logs = {}
        self.outages = {}
        for label in ('public','alchemy'):
            for kind in ('result_unavailable','errors','cooldown_skips'):
                self.counts[label+'_'+kind] = 0

    @staticmethod
    def error_policy(method, exc):
        """Return a safe label and whether the entire provider should cool down."""
        if not isinstance(exc,RpcError):
            return 'invalid-response',False
        reason=str(exc)
        if reason=='endpoint-cooling':
            # Rpc already owns the endpoint's Retry-After deadline.
            return reason,False
        match=re.fullmatch(re.escape(method)+r':RPC-(-?\d{1,6})',reason)
        if match:
            code=int(match[1])
            # Missing block/context, unsupported request/version and invalid
            # parameters do not establish an outage of unrelated reads.
            return 'RPC-'+str(code),code not in (-32004,-32007,-32009,-32014,-32015,-32016,-32600,-32601,-32602)
        if re.fullmatch(r'HTTP-[1-5]\d{2}',reason):
            return reason,True
        if reason in (method+':TimeoutError',method+':ConnectionTimeoutError',
                      method+':SocketTimeoutError',method+':ClientConnectorError',
                      method+':ClientConnectionError',method+':ServerDisconnectedError'):
            return reason.split(':',1)[1],True
        # Unknown exception text can contain URLs/credentials; never forward it.
        return 'rpc-error',True

    def record_failure(self,label,method,reason,scope):
        detail={'method':method,'reason':reason,'scope':scope}
        self.last_failure[label]=detail
        key=(label,method,reason,scope)
        now=self.clock()
        if now-self.failure_logs.get(key,float('-inf'))>=60:
            self.failure_logs[key]=now
            logging.getLogger(__name__).info('[SOL] RPC read deferred provider=%s method=%s reason=%s scope=%s',
                                             label,method,reason,scope)

    @staticmethod
    def valid(method, result, params):
        if method == 'getTransaction':
            if not isinstance(result, dict) or type(result.get('slot')) is not int or result['slot'] < 0:
                return False
            meta, tx = result.get('meta'), result.get('transaction')
            if not isinstance(meta, dict) or 'err' not in meta or not isinstance(tx, dict):
                return False
            message = tx.get('message')
            version = result.get('version')
            return (isinstance(message, dict) and isinstance(message.get('accountKeys'), list)
                    and bool(message['accountKeys']) and isinstance(tx.get('signatures'), list)
                    and bool(tx['signatures']) and tx['signatures'][0] == params[0]
                    and (version is None or version == 'legacy'
                         or type(version) is int and 0 <= version <= 1))
        if method == 'getSignaturesForAddress':
            if not isinstance(result, list):
                return False
            limit = (params[1] if len(params) > 1 else {}).get('limit', 1000)
            if len(result) > limit:
                return False
            signatures = set()
            last = None
            for row in result:
                if (not isinstance(row, dict) or not isinstance(row.get('signature'), str)
                        or not row['signature'] or row['signature'] in signatures
                        or type(row.get('slot')) is not int or row['slot'] < 0
                        or 'err' not in row or last is not None and row['slot'] > last):
                    return False
                signatures.add(row['signature'])
                last = row['slot']
            return True  # A complete empty history is valid, not a provider error.
        if method == 'getAccountInfo':
            if not isinstance(result, dict) or not isinstance(result.get('context'), dict):
                return False
            slot = result['context'].get('slot')
            options = params[1] if len(params) > 1 else {}
            if type(slot) is not int or slot < options.get('minContextSlot', 0) or 'value' not in result:
                return False
            row = result['value']
            # A missing/closed account is a valid result; qualification rejects it.
            return row is None or (isinstance(row, dict) and isinstance(row.get('owner'), str)
                                  and type(row.get('executable')) is bool
                                  and isinstance(row.get('data'), list) and len(row['data']) == 2)
        if method == 'getBlockTime':
            return type(result) is int and result > 0
        return False

    async def call(self, method, params):
        if method not in self.METHODS:
            # Only explicitly supported reads can use this reader's fallback.
            return await self.public.call(method, params)
        self.counts['calls'] += 1
        providers=[('public', self.public), ('alchemy', self.alchemy)]
        if self.prefer_alchemy and self.alchemy is not None:providers.reverse()
        for index,(label, client) in enumerate(providers):
            if client is None:
                continue
            async with self.slots[label]:
                # Recheck after waiting: an earlier request may have cooled this endpoint.
                if self.clock() < self.cooldown.get(label, 0):
                    self.counts[label+'_cooldown_skips'] += 1
                    continue
                generation = self.generation[label]
                self.counts[label + '_attempts'] += 1
                try:
                    with defer_429_alerts(self.alchemy is not None):
                        result = await client.call(method, params)
                    if not self.valid(method, result, params):
                        # A null/unusable result only blocks this request. Other
                        # signatures, accounts and methods can still succeed.
                        self.counts[label+'_result_unavailable'] += 1
                        self.record_failure(label,method,'result-null' if result is None else 'invalid-result','request')
                        continue
                except (RpcError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    reason,cool_provider=self.error_policy(method,exc)
                    self.counts[label+('_cooldown_skips' if reason=='endpoint-cooling' else '_errors')] += 1
                    self.record_failure(label,method,reason,
                                        'provider' if cool_provider else 'endpoint' if reason=='endpoint-cooling' else 'request')
                    if not cool_provider:
                        continue
                    # Concurrent failures from one wave count as one cooldown transition.
                    if generation == self.generation[label]:
                        self.generation[label] += 1
                        failures = min(4, self.failures.get(label, 0) + 1)
                        self.failures[label] = failures
                        self.cooldown[label] = self.clock() + min(120, 15 * 2**failures)
                    continue
                # An older successful request must not erase a newer failure's cooldown.
                if generation == self.generation[label]:
                    self.failures.pop(label, None)
                    self.cooldown.pop(label, None)
                if index:
                    self.counts['recovered'] += 1
                # Either provider returning a valid result restores this method.
                self.outages.pop(method,None)
                return result
        self.counts['unavailable'] += 1
        raise self.unavailable(method) from None

    def unavailable(self, method):
        if self.alchemy is not None:
            now=self.clock()
            outage=self.outages.setdefault(method,{'since':now,'notified':False})
            alert=now-outage['since']>60 and not outage['notified']
            report_funding_unavailable(method,alert=alert)
            if alert:outage['notified']=True
        return RpcError('funding-rpc-unavailable')

    async def transaction(self, signature, commitment='confirmed'):
        return await self.call('getTransaction', [signature, {'encoding': 'jsonParsed',
            'commitment': commitment, 'maxSupportedTransactionVersion': 1}])

    def snapshot(self):
        return {**self.counts,'last_failure':{label:dict(detail) for label,detail in self.last_failure.items()},
                'cooldown_remaining_s':{label:round(max(0,until-self.clock()),1)
                                        for label,until in self.cooldown.items()}}
