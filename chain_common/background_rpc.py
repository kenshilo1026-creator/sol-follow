"""Rate-budgeted public RPC reads. No paid HTTP fallback."""
import asyncio
import time


class BackgroundRpc:
    ALLOWED = {'getSignaturesForAddress', 'getSignatureStatuses', 'getTransaction',
               'getBlockTime', 'getAccountInfo', 'getMultipleAccounts', 'getBalance','getBlockHeight',
               'getTokenAccountsByOwner'}

    def __init__(self, public, priority, rps=2):
        self.public, self.priority = public, priority
        self.lock = asyncio.Lock()
        self.interval = 1 / rps
        self.next_request = 0.0

    async def call(self, method, params):
        if method not in self.ALLOWED:
            raise ValueError('background-rpc-read-method-required')
        async with self.lock:
            while True:
                if self.priority.active:
                    await asyncio.sleep(0.05)
                    continue
                delay=self.next_request-time.monotonic()
                if delay>0:
                    await asyncio.sleep(delay)
                    continue  # A trade may have started while waiting for the rate budget.
                self.next_request = time.monotonic() + self.interval
                return await self.public.call(method, params)

    async def transaction(self, signature, commitment='confirmed'):
        return await self.call('getTransaction', [signature, {'encoding':'jsonParsed',
            'commitment':commitment, 'maxSupportedTransactionVersion':1}])

    def snapshot(self):
        return self.public.snapshot()
