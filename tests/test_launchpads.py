import base64
import copy
import json
from pathlib import Path
import time

import pytest

from chain_common.primitives import SYSTEM, WSOL, pubkey
from chain_common.transaction import Unsupported
from launchpads import ENABLED, LaunchpadScope, pump_fun, stonk
from tests.helpers import address

FIXTURE = json.loads((Path(__file__).parent/'fixtures/launchpad_origins_mainnet.json').read_text())
STONK_POOL = '5fXSWBWuNx64qxs19orjnpDMCXoMUe7R5ZEx8fZxXE5v'


class OriginRPC:
    def __init__(self, mint, launchpad=None):
        self.mint = mint
        self.launchpad = launchpad
        self.calls = []
        self.slot = 500_000_000
        self.curve = copy.deepcopy(FIXTURE[pump_fun.curve_address(pump_fun.EXAMPLE_MINT)]['value'])
        self.platform = copy.deepcopy(FIXTURE[stonk.PLATFORM]['value'])
        row = copy.deepcopy(FIXTURE[STONK_POOL]['value'])
        raw = bytearray(base64.b64decode(row['data'][0]))
        raw[205:237] = bytes(pubkey(mint))
        self.quote = WSOL
        raw[237:269] = bytes(pubkey(self.quote))
        row['data'][0] = base64.b64encode(raw).decode()
        self.pools = [{'pubkey':stonk.pool_address(mint,self.quote),'account':row}]

    async def call(self, method, params):
        self.calls.append((method,params))
        if method == 'getAccountInfo':
            if params[0] == stonk.PLATFORM:
                value = self.platform
            else:
                assert params[0] == pump_fun.curve_address(self.mint)
                value = self.curve if self.launchpad == pump_fun.NAME else None
        elif method == 'getProgramAccounts':
            assert params[0] == stonk.PROGRAM
            assert params[1]['filters'][1]['memcmp']['bytes'] == stonk.PLATFORM
            assert params[1]['filters'][2]['memcmp']['bytes'] == self.mint
            value = self.pools if self.launchpad == stonk.NAME else []
        else:
            raise AssertionError(method)
        return {'context':{'slot':self.slot},'value':value}


@pytest.mark.parametrize('launchpad', ENABLED)
@pytest.mark.asyncio
async def test_other_mints_on_allowed_platform_are_accepted(launchpad):
    mint = address()
    assert mint not in (pump_fun.EXAMPLE_MINT, stonk.EXAMPLE_MINT)
    rpc = OriginRPC(mint,launchpad)
    scope = LaunchpadScope()
    assert (await scope.require(rpc,mint)).launchpad == launchpad
    calls = len(rpc.calls)
    assert (await scope.require(rpc,mint)).launchpad == launchpad
    assert len(rpc.calls) == calls


def test_real_sample_account_proofs():
    assert ENABLED == ('pump.fun','stonk')
    assert pump_fun.verify(FIXTURE[pump_fun.curve_address(pump_fun.EXAMPLE_MINT)]['value'])
    assert stonk.verify_platform(FIXTURE[stonk.PLATFORM]['value'])
    assert stonk.verify_pool(STONK_POOL,FIXTURE[STONK_POOL]['value'],stonk.EXAMPLE_MINT)


@pytest.mark.parametrize('bad', ['owner','discriminator','platform','mint','pda'])
@pytest.mark.asyncio
async def test_stonk_rejects_foreign_or_forged_proof(bad):
    mint = address()
    rpc = OriginRPC(mint,stonk.NAME)
    row = rpc.pools[0]
    raw = bytearray(base64.b64decode(row['account']['data'][0]))
    if bad == 'owner': row['account']['owner'] = SYSTEM
    elif bad == 'discriminator': raw[:8] = bytes(8)
    elif bad == 'platform': raw[173:205] = bytes(pubkey(address()))
    elif bad == 'mint': raw[205:237] = bytes(pubkey(address()))
    elif bad == 'pda': row['pubkey'] = address()
    row['account']['data'][0] = base64.b64encode(raw).decode()
    with pytest.raises(Unsupported,match='outside'):
        await LaunchpadScope().require(rpc,mint)


@pytest.mark.asyncio
async def test_pump_suffix_and_wrong_owner_are_not_proof():
    mint = pump_fun.EXAMPLE_MINT
    rpc = OriginRPC(mint,pump_fun.NAME)
    rpc.curve['owner'] = SYSTEM
    assert await LaunchpadScope().classify(rpc,mint) is None


@pytest.mark.asyncio
async def test_platform_change_and_rpc_failure_are_not_cached_as_absence():
    mint = address()
    rpc = OriginRPC(mint,stonk.NAME)
    rpc.platform['owner'] = SYSTEM
    scope = LaunchpadScope()
    with pytest.raises(Unsupported,match='platform-proof'):
        await scope.require(rpc,mint)
    assert not scope.cache
    rpc.platform['owner'] = stonk.PROGRAM
    assert (await scope.require(rpc,mint)).launchpad == stonk.NAME
    class Broken:
        async def call(self,*args):raise TimeoutError()
    with pytest.raises(TimeoutError):
        await scope.classify(Broken(),address())
    assert len(scope.cache)==1


@pytest.mark.asyncio
async def test_old_context_rejected_and_cache_bounded():
    scope = LaunchpadScope(capacity=1)
    for _ in range(2):
        mint = address()
        rpc = OriginRPC(mint,pump_fun.NAME)
        await scope.require(rpc,mint)
    assert len(scope.cache)==1
    with pytest.raises(Unsupported,match='context-too-old'):
        await scope.classify(rpc,mint,min_slot=rpc.slot+1)
