"""Allowed token origins, separate from supported trade execution routes."""
from collections import OrderedDict
from dataclasses import dataclass
import time

from chain_common.primitives import pubkey
from chain_common.transaction import Unsupported
from launchpads import pump_fun, stonk

ENABLED = (pump_fun.NAME, stonk.NAME)


@dataclass(frozen=True)
class Origin:
    launchpad: str
    account: str
    slot: int


class LaunchpadScope:
    """Bounded cache of RPC-verified origin; failed RPCs are never negative proof."""

    def __init__(self, capacity=4096):
        self.cache = OrderedDict()
        self.capacity = capacity

    async def classify(self, rpc, mint, *, min_slot=0):
        pubkey(mint)
        cached = self.cache.get(mint)
        if cached and cached[0] > time.monotonic() and cached[1] >= min_slot:
            self.cache.move_to_end(mint)
            return cached[2]
        curve = pump_fun.curve_address(mint)
        options = {'encoding': 'base64', 'commitment': 'confirmed', 'minContextSlot': min_slot}
        result = await rpc.call('getAccountInfo', [curve, options])
        slot = result['context']['slot']
        if slot < min_slot:
            raise Unsupported('launchpad-proof-context-too-old')
        if pump_fun.verify(result['value']):
            origin = Origin(pump_fun.NAME, curve, slot)
        else:
            # Filter by both base mint and the verified Stonk platform. Never
            # equate all LaunchLab or Raydium tokens with Stonk.
            result = await rpc.call('getProgramAccounts', [stonk.PROGRAM, {
                **options, 'withContext': True,
                'filters': [{'dataSize': 429},
                            {'memcmp': {'offset': 173, 'bytes': stonk.PLATFORM}},
                            {'memcmp': {'offset': 205, 'bytes': mint}}],
            }])
            slot = result['context']['slot']
            if slot < min_slot:
                raise Unsupported('launchpad-proof-context-too-old')
            matches = [row['pubkey'] for row in result['value']
                       if stonk.verify_pool(row['pubkey'], row['account'], mint)]
            origin = None
            if matches:
                platform = await rpc.call('getAccountInfo', [stonk.PLATFORM, {
                    **options, 'minContextSlot': slot,
                }])
                if platform['context']['slot'] < slot:
                    raise Unsupported('launchpad-proof-context-too-old')
                if not stonk.verify_platform(platform['value']):
                    raise Unsupported('stonk-platform-proof-invalid')
                origin = Origin(stonk.NAME, sorted(matches)[0], slot)
        self.cache[mint] = (time.monotonic() + (300 if origin else 15), slot, origin)
        self.cache.move_to_end(mint)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return origin

    async def require(self, rpc, mint, *, min_slot=0):
        origin = await self.classify(rpc, mint, min_slot=min_slot)
        if origin is None:
            raise Unsupported('token-outside-pump-fun-stonk-scope')
        return origin
