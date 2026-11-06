"""Bounded funding concurrency and transaction reuse within one job."""
import asyncio
import copy
import json
import time
from collections import OrderedDict
from features.funding.rpc import FundingRpc
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
    """Bounded confirmed receipt reuse within one funding job, never verdicts."""
    MAX_ENTRIES = 32
    MAX_BYTES = 4 * 1024 * 1024
    TTL = 10

    def __init__(self, rpc, raw, *, clock=time.monotonic, stats=None):
        self.rpc = rpc
        self.raw = raw
        self.signature = raw['transaction']['signatures'][0]
        self.clock = clock
        self.stats = stats if stats is not None else {}
        self.receipts = OrderedDict()
        self.bytes = 0

    def count(self, name):
        self.stats[name] = self.stats.get(name, 0) + 1

    async def call(self, method, params):
        return await self.rpc.call(method, params)

    async def transaction(self, signature, commitment='confirmed'):
        if signature == self.signature and commitment == 'confirmed':
            self.count('deposit_reused')
            return self.raw
        # Never promote a confirmed receipt to finalized or retain processed data.
        if commitment != 'confirmed':
            return await self.rpc.transaction(signature, commitment=commitment)
        now = self.clock()
        for key, (at, raw, size) in list(self.receipts.items()):
            if now - at >= self.TTL or now < at:
                self.receipts.pop(key)
                self.bytes -= size
        if signature in self.receipts:
            self.count('history_reused')
            return copy.deepcopy(self.receipts[signature][1])
        self.count('history_requested')
        raw = await self.rpc.transaction(signature)
        if (FundingRpc.valid('getTransaction', raw, [signature])
                and raw.get('_stream', {}).get('commitment') != 'processed'):
            size = len(json.dumps(raw).encode('utf-8'))
            if size <= self.MAX_BYTES:
                while self.receipts and (len(self.receipts) >= self.MAX_ENTRIES
                                        or self.bytes + size > self.MAX_BYTES):
                    _, (_, _, removed) = self.receipts.popitem(last=False)
                    self.bytes -= removed
                self.receipts[signature] = (self.clock(), copy.deepcopy(raw), size)
                self.bytes += size
        return raw
