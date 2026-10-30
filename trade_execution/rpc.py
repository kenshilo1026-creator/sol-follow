"""Independent execution/read lanes; fallback never constructs a transaction."""
import asyncio
import base64
import copy
import time

from solders.transaction import VersionedTransaction
from solders.hash import Hash
from chain_common.public_rpc import defer_429_alerts
from chain_common.rpc import RpcError
from features.funding.confirmation_rpc import ConfirmationRpc
from features.funding.rpc import FundingRpc

MAINNET = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
READ_METHODS = {'getSignatureStatuses', 'getTransaction', 'getBlockHeight'}


class ExecutionRpc:
    def __init__(self, primary, fallback=None, *, notices=None, lane='execution', reads=None, hedge_ms=0):
        self.hedge_ms = hedge_ms
        self.send_hedges = self.send_primary_wins = self.send_fallback_wins = 0
        self.providers = [primary] + ([fallback] if fallback is not None else [])
        self.notices, self.lane, self.reads = notices, lane, reads
        self.verified = set()
        self.rejected = set()
        self.locks = [asyncio.Lock() for _ in self.providers]
        self.recovered = self.unavailable = 0
        self.outages = {}
        self.cooldown = {}

    @property
    def calls(self):
        return sum(p.snapshot()['calls'] for p in self.providers)

    @property
    def limited(self):
        return sum(p.snapshot()['rate_limited'] for p in self.providers)

    def snapshot(self):
        return {'send_hedges':self.send_hedges,'send_primary_wins':self.send_primary_wins,
                'send_fallback_wins':self.send_fallback_wins,'calls': self.calls, 'rate_limited': self.limited,
                'fallback_recovered': self.recovered, 'unavailable': self.unavailable,
                'providers': len(self.providers), 'verified_providers': len(self.verified),
                'cooldown_remaining_s': [round(max(0,self.cooldown.get(i,0)-time.monotonic()),1)
                                         for i in range(len(self.providers))]}

    async def verify(self, index):
        async with self.locks[index]:
            if time.monotonic() < self.cooldown.get(index, 0):
                raise RpcError('endpoint-cooling')
            if index in self.rejected:
                raise RpcError('execution-provider-wrong-chain')
            if index not in self.verified:
                genesis=await self.providers[index].call('getGenesisHash', [])
                if not isinstance(genesis,str):
                    raise RpcError('execution-genesis-unavailable')
                Hash.from_string(genesis)
                if genesis != MAINNET:
                    self.rejected.add(index)
                    raise RpcError('execution-provider-wrong-chain')
                self.verified.add(index)

    @staticmethod
    def valid(method, result, params, signature):
        if method == 'sendTransaction':
            return result == signature
        if method == 'getSignatureStatuses':
            return ConfirmationRpc.valid(method, result, params)
        if method == 'getTransaction':
            return result is None or FundingRpc.valid(method, result, params)
        if method in FundingRpc.METHODS:
            return FundingRpc.valid(method, result, params)
        if method in ('getBlockHeight', 'getSlot', 'getMinimumBalanceForRentExemption'):
            return type(result) is int and result >= 0
        if method == 'getGenesisHash':
            return result == MAINNET
        if method == 'simulateTransaction':
            return (isinstance(result, dict) and isinstance(result.get('value'), dict)
                    and 'err' in result['value'] and isinstance(result.get('context'), dict)
                    and type(result['context'].get('slot')) is int
                    and result['context']['slot'] >= params[1].get('minContextSlot', 0))
        return result is not None

    def failure(self, method):
        self.unavailable += 1
        since, notified = self.outages.setdefault(method, (time.monotonic(), False))
        alert = time.monotonic()-since > 60 and not notified
        if alert:
            self.outages[method] = (since, True)
        if self.notices:
            self.notices.emit('trading RPC unavailable', {'lane': self.lane, 'method': method},
                alert=alert, key=f'rpc-{self.lane}-{method}-'+('alert' if alert else 'local'), interval=60)

    async def send_hedged(self, params, signature):
        # Every attempt broadcasts the same durable signed bytes. Losing an ACK
        # is ambiguous, never permission to build/sign a replacement transaction.
        async def attempt(index):
            try:
                await self.verify(index)
                result=await self.providers[index].call('sendTransaction',copy.deepcopy(params))
                return result if self.valid('sendTransaction',result,params,signature) else None
            except asyncio.CancelledError:
                raise
            except (RpcError,asyncio.TimeoutError,ValueError,TypeError,KeyError,IndexError,AttributeError) as exc:
                reason=str(exc) if isinstance(exc,RpcError) else ''
                if (isinstance(exc,asyncio.TimeoutError) or reason.startswith('HTTP-5')
                    or reason.endswith((':TimeoutError',':ClientConnectorError',':ServerDisconnectedError'))):
                    self.cooldown[index]=time.monotonic()+5
                return None

        tasks={}
        def start(index):
            task=asyncio.create_task(attempt(index));tasks[task]=index
            return task
        def accepted(task):
            result=task.result()
            if result is None:return None
            index=tasks[task]
            if index:
                self.send_fallback_wins+=1;self.recovered+=1
            else:self.send_primary_wins+=1
            self.outages.pop('sendTransaction',None)
            return result
        with defer_429_alerts():
            try:
                primary=start(0)
                done,_=await asyncio.wait({primary},timeout=self.hedge_ms/1000)
                if done:
                    result=accepted(primary)
                    if result is not None:return result
                else:self.send_hedges+=1
                backup=start(1)
                pending={backup} | (set() if done else {primary})
                while pending:
                    done,pending=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                    for task in sorted(done,key=lambda task:tasks[task]):
                        result=accepted(task)
                        if result is not None:return result
                self.failure('sendTransaction')
                raise RpcError('execution-rpc-unavailable')
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)

    async def call(self, method, params):
        if not method.startswith('get') and method not in ('simulateTransaction','sendTransaction'):
            raise ValueError('execution-rpc-method-not-supported')
        if self.reads is not None and method in READ_METHODS:
            return await self.reads.call(method, params)
        # Freeze the caller's request before any await. In particular the signed
        # bytes, blockhash, minContextSlot and preflight policy cannot change.
        params = copy.deepcopy(params)
        signature = None
        if method == 'sendTransaction':
            if len(params) != 2 or params[1].get('encoding') != 'base64':
                raise ValueError('execution-send-requires-base64')
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0], validate=True))
            tx.verify_and_hash_message()
            signature = str(tx.signatures[0])
            if self.hedge_ms>0 and len(self.providers)>1:
                return await self.send_hedged(params,signature)
        missing = None
        with defer_429_alerts():
            for index, provider in enumerate(self.providers):
                if time.monotonic() < self.cooldown.get(index, 0):
                    continue
                try:
                    await self.verify(index)
                    result = MAINNET if method == 'getGenesisHash' else await provider.call(method, copy.deepcopy(params))
                    if not self.valid(method, result, params, signature):
                        raise RpcError('execution-invalid-result')
                    if method == 'getSignatureStatuses' and missing is not None:
                        # A null from the backup must never erase evidence already
                        # observed on the primary (including on-chain failure).
                        result = {**result, 'context': {'slot': max(result['context']['slot'], missing['context']['slot'])},
                                  'value': [a if a is not None else b for a, b in zip(missing['value'], result['value'])]}
                    absent = (method == 'getTransaction' and result is None or
                              method == 'getSignatureStatuses' and any(r is None for r in result['value']))
                    if absent and index+1 < len(self.providers):
                        missing = result
                        continue
                    self.outages.pop(method, None)
                    if index:
                        self.recovered += 1
                    return result
                except asyncio.CancelledError:
                    raise
                except (RpcError, asyncio.TimeoutError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                    reason=str(exc) if isinstance(exc,RpcError) else ''
                    # Short circuit only transport/server failures, not a null,
                    # lagging slot, wrong signature or invalid single response.
                    if (isinstance(exc,asyncio.TimeoutError) or reason.startswith('HTTP-5')
                            or reason.endswith((':TimeoutError',':ClientConnectorError',':ServerDisconnectedError'))):
                        self.cooldown[index]=time.monotonic()+5
                    continue
        # Unknown is valid status evidence, never proof of expiry or permission
        # to re-sign. A missing transaction instead remains retryable.
        if missing is not None:
            self.outages.pop(method, None)
            return missing
        self.failure(method)
        raise RpcError('execution-rpc-unavailable') from None

    async def transaction(self, signature, commitment='confirmed'):
        return await self.call('getTransaction', [signature, {'encoding': 'jsonParsed',
            'commitment': commitment, 'maxSupportedTransactionVersion': 1}])
