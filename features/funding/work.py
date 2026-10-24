"""Bounded funding concurrency and transaction reuse within one job."""
import asyncio
from contextlib import asynccontextmanager


class WalletFlights:
    def __init__(self):
        self.entries = {}

    @asynccontextmanager
    async def hold(self, wallet):
        entry = self.entries.setdefault(wallet, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self.entries[wallet]


class TransactionReads:
    """Reuse this job's confirmed transaction; never cache account/history verdicts."""
    def __init__(self, rpc, raw):
        self.rpc = rpc
        self.raw = raw
        self.signature = raw['transaction']['signatures'][0]

    async def call(self, method, params):
        return await self.rpc.call(method, params)

    async def transaction(self, signature, commitment='confirmed'):
        if signature == self.signature and commitment == 'confirmed':
            return self.raw
        if commitment == 'confirmed':
            return await self.rpc.transaction(signature)
        return await self.rpc.transaction(signature, commitment=commitment)
