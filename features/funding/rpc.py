"""Bounded public-first reads for funding qualification, never execution."""
import asyncio
import time

from chain_common.rpc import RpcError
from chain_common.public_rpc import defer_429_alerts, report_funding_unavailable


class FundingRpc:
    METHODS = {'getTransaction', 'getAccountInfo', 'getSignaturesForAddress', 'getBlockTime'}

    def __init__(self, public, alchemy=None, *, clock=time.monotonic):
        self.public, self.alchemy, self.clock = public, alchemy, clock
        self.slots = {"public": asyncio.Semaphore(1), "alchemy": asyncio.Semaphore(4)}
        self.generation = {"public": 0, "alchemy": 0}
        self.cooldown = {}
        self.failures = {}
        self.counts = dict(calls=0, public_attempts=0, alchemy_attempts=0,
                           recovered=0, unavailable=0)

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
        for label, client in (('public', self.public), ('alchemy', self.alchemy)):
            if client is None:
                continue
            async with self.slots[label]:
                # Recheck after waiting: an earlier request may have cooled this endpoint.
                if self.clock() < self.cooldown.get(label, 0):
                    continue
                generation = self.generation[label]
                self.counts[label + '_attempts'] += 1
                try:
                    with defer_429_alerts(self.alchemy is not None):
                        result = await client.call(method, params)
                    if not self.valid(method, result, params):
                        raise RpcError('funding-result-unavailable')
                except (RpcError, ValueError, TypeError, KeyError, AttributeError):
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
                if label == 'alchemy':
                    self.counts['recovered'] += 1
                return result
        self.counts['unavailable'] += 1
        raise self.unavailable(method) from None

    def unavailable(self, method):
        if self.alchemy is not None:
            report_funding_unavailable(method)
        return RpcError('funding-rpc-unavailable')

    async def transaction(self, signature, commitment='confirmed'):
        return await self.call('getTransaction', [signature, {'encoding': 'jsonParsed',
            'commitment': commitment, 'maxSupportedTransactionVersion': 1}])

    def snapshot(self):
        return dict(self.counts)
