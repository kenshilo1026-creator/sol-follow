"""Funding recovery uses fake providers; fixtures never read production CEX files."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from chain_common.background_rpc import BackgroundRpc
from chain_common.primitives import SYSTEM
from chain_common.rpc import Priority, RpcError
from features.database.storage import Store
from features.funding.decoder import Funding
from features.funding.rpc import FundingRpc
from features.runtime.service import Service
from share_common.config import load
from tests.helpers import address, transaction
from tests.test_funding_history_rpc import (
    test_discovery_routing_keeps_details_public_and_history_nonlive,
    test_both_history_providers_fail_retains_request_and_keeps_alert_local,
    test_public_cooldown_does_not_block_alchemy_history,
)


@pytest.fixture
def config(tmp_path):
    (tmp_path/'cex_addresses.json').write_text(json.dumps({'exchanges': {'test': [address()]}}))
    return replace(load(tmp_path, env={'DRY_RUN': 'true', 'SOL_FEED_MODE': 'websocket'}),
                   max_dev_holding_tokens=None)


@pytest.fixture
def store(config):
    return Store(config.data)


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        result = self.replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def parsed(sig='fund', wallet=None):
    return transaction([wallet or address()], [], [], sig=sig, slot=1000,
                       when=int(time.time()))


def account(slot=1000):
    return {'context': {'slot': slot}, 'value': {'owner': SYSTEM,
            'executable': False, 'data': ['', 'base64']}}


@pytest.mark.asyncio
async def test_healthy_public_does_not_spend_alchemy():
    value=parsed(); public=Client(value); alchemy=Client()
    reader=FundingRpc(public,alchemy)
    assert await reader.transaction('fund') is value
    assert alchemy.calls==[]
    assert reader.snapshot()['alchemy_attempts']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, {}, False, {'slot':1000},
    RpcError('HTTP-429'), RpcError('getTransaction:TimeoutError'),
    RpcError('getTransaction:RPC--32005')])
async def test_transaction_missing_or_provider_error_uses_one_recovery(bad):
    value=parsed(); public=Client(bad); alchemy=Client(value)
    reader=FundingRpc(public,alchemy)
    assert await reader.transaction('fund') is value
    assert public.calls==alchemy.calls
    assert len(alchemy.calls)==1
    assert reader.snapshot()['recovered']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('method,params,bad,good', [
    ('getAccountInfo',['wallet',{'minContextSlot':1000}], account(999), account()),
    ('getAccountInfo',['wallet',{}], {'value':False}, account()),
    ('getSignaturesForAddress',['wallet',{'limit':2}], None, []),
    ('getSignaturesForAddress',['wallet',{'limit':2}],
     [{'signature':'same','slot':1000,'err':None}]*2, []),
    ('getSignaturesForAddress',['wallet',{'limit':1}],
     [{'signature':'a','slot':1000,'err':None},{'signature':'b','slot':999,'err':None}], []),
    ('getBlockTime',[1000], None, 1790000000),
    ('getBlockTime',[1000], True, 1790000000),
])
async def test_other_qualification_reads_validate_before_recovery(method,params,bad,good):
    public=Client(bad); alchemy=Client(good); reader=FundingRpc(public,alchemy)
    assert await reader.call(method,params)==good
    assert public.calls==alchemy.calls==[(method,params)]


@pytest.mark.asyncio
@pytest.mark.parametrize('method,params,value',[
    ('getSignaturesForAddress',['wallet',{'limit':1}], []),
    ('getAccountInfo',['wallet',{}], {'context':{'slot':1000},'value':None}),
])
async def test_valid_negative_results_do_not_trigger_recovery(method,params,value):
    public=Client(value); alchemy=Client(); reader=FundingRpc(public,alchemy)
    assert await reader.call(method,params)==value
    assert alchemy.calls==[]


@pytest.mark.asyncio
async def test_both_fail_and_cooling_never_returns_zero_history_or_leaks_secret():
    now=[0.0]; public=Client(RpcError('https://example/secret')); alchemy=Client(RpcError('HTTP-500'))
    reader=FundingRpc(public,alchemy,clock=lambda:now[0])
    for _ in range(2):
        with pytest.raises(RpcError,match='^funding-rpc-unavailable$'):
            await reader.transaction('fund')
    assert len(public.calls)==len(alchemy.calls)==1
    assert reader.snapshot()['unavailable']==2
    assert 'secret' not in json.dumps(reader.snapshot())
    now[0]=30
    value=parsed(); public.replies.append(value)
    assert await reader.transaction('fund') is value
    assert len(public.calls)==2 and len(alchemy.calls)==1


@pytest.mark.asyncio
async def test_failure_cooldown_increases_but_is_bounded():
    now=[0.0]; public=Client(*[RpcError('failed') for _ in range(6)])
    reader=FundingRpc(public,clock=lambda:now[0])
    for delay in [30,60,120,120,120,120]:
        with pytest.raises(RpcError): await reader.transaction('fund')
        assert reader.cooldown['public']-now[0]==delay
        now[0]+=delay


@pytest.mark.asyncio
async def test_no_key_keeps_failure_pending_without_alchemy():
    reader=FundingRpc(Client(None))
    with pytest.raises(RpcError,match='funding-rpc-unavailable'):
        await reader.transaction('fund')
    assert reader.snapshot()['alchemy_attempts']==0


@pytest.mark.asyncio
async def test_cancellation_does_not_trigger_recovery():
    alchemy=Client(); reader=FundingRpc(Client(asyncio.CancelledError()),alchemy)
    with pytest.raises(asyncio.CancelledError): await reader.transaction('fund')
    assert alchemy.calls==[]


@pytest.mark.asyncio
async def test_wrong_signature_or_version_is_not_accepted():
    wrong=parsed('other'); wrong_version=parsed(); wrong_version['version']=2
    reader=FundingRpc(Client(wrong),Client(wrong_version))
    with pytest.raises(RpcError,match='funding-rpc-unavailable'):
        await reader.transaction('fund')


@pytest.mark.asyncio
async def test_status_reconciliation_stays_public_without_recovery():
    public=Client(RpcError('HTTP-429')); alchemy=Client()
    reader=FundingRpc(public,alchemy)
    with pytest.raises(RpcError,match='HTTP-429'):
        await reader.call('getSignatureStatuses',[['fund']])
    assert alchemy.calls==[]


@pytest.mark.asyncio
async def test_recovery_waits_for_trade_priority():
    priority=Priority(); public=Client(RpcError('HTTP-429')); alchemy=Client(parsed())
    class PauseAfterFailure(Client):
        async def call(self, method, params):
            priority.active=1
            return await public.call(method,params)
    reader=FundingRpc(BackgroundRpc(PauseAfterFailure(),priority,1000),
                      BackgroundRpc(alchemy,priority,1000))
    task=asyncio.create_task(reader.transaction('fund'))
    await asyncio.sleep(0.02)
    assert alchemy.calls==[] and not task.done()
    priority.active=0
    await asyncio.wait_for(task,0.5)
    assert len(alchemy.calls)==1


@pytest.mark.asyncio
async def test_real_qualification_recovery_preserves_account_and_history_checks(config,store):
    wallet=address(); stamp=int(time.time())
    item=Funding('event','fund',wallet,address(),'cex:test',10**9,1000,stamp)
    rows=[{'signature':'fund','slot':1000,'blockTime':stamp,'err':None}]
    public=Client(RpcError('HTTP-429'))
    alchemy=Client(account(),rows,parsed(wallet=wallet))
    reader=FundingRpc(public,alchemy)
    service=Service(config,store)
    assert await service.qualify(item,reader)
    assert [m for m,p in alchemy.calls]==['getAccountInfo','getSignaturesForAddress','getTransaction']
    assert reader.snapshot()['recovered']==3


@pytest.mark.asyncio
async def test_unknown_account_cannot_admit(config,store):
    item=Funding('event','fund',address(),address(),'cex:test',10**9,1000,int(time.time()))
    reader=FundingRpc(Client(None),Client(None))
    with pytest.raises(RpcError,match='funding-rpc-unavailable'):
        await Service(config,store).qualify(item,reader)
    assert store.rows('funding','SELECT * FROM hotlist')==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['','fixture-secret'])
async def test_runtime_funding_reader_is_separate_from_public_execution(config,store,monkeypatch,key):
    from tests.test_funding_history_rpc import wired_service
    service,discovery,calls,response=await wired_service(config,store,monkeypatch,key)
    assert service.funding_rpc.public is service.background_rpc
    assert service.funding_rpc.alchemy is (service.funding_history_rpc if key else None)
    assert discovery.rpc is service.source_history_rpc
    async def json_result(self):
        return {'result':parsed()}
    monkeypatch.setattr(response,'json',json_result)
    service.priority.cooldown[service.config.rpc]=time.monotonic()+30
    prior=len(calls)
    if key:
        assert (await service.funding_rpc.transaction('fund'))['slot']==1000
        assert len(calls)==prior+1
        assert calls[-1][0]==service.config.funding_history_url
        assert calls[-1][1]['method']=='getTransaction'
    else:
        with pytest.raises(RpcError,match='funding-rpc-unavailable'):
            await service.funding_rpc.transaction('fund')
        assert len(calls)==prior
    prior=len(calls)
    with pytest.raises(RpcError,match='endpoint-cooling'):
        await service.background_rpc.transaction('fund')
    assert len(calls)==prior  # The single-provider funding adapter remains cooled.
    assert service.execution_rpc.providers[0].priority is not service.priority
