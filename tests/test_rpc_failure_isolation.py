"""A missing result must not stop unrelated funding reads for an entire provider."""
import asyncio
import json
import time

import pytest

from chain_common.rpc import Priority, Rpc, RpcError
from features.funding.rpc import FundingRpc
from tests.test_funding_rpc_recovery import Client, account, parsed
from tests.test_rpc_alerts import Reply, Session


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[None,{},False,parsed('wrong-signature'),
    RpcError('getTransaction:RPC--32004'),RpcError('getTransaction:RPC--32015'),
    ValueError('https://provider/fixture-secret'),TypeError('fixture-secret')])
async def test_one_bad_transaction_does_not_cool_alchemy_or_other_methods(bad):
    public=Client(RpcError('HTTP-429'))
    good=parsed('another-fund')
    alchemy=Client(bad,account(),[],good)
    reader=FundingRpc(public,alchemy,clock=lambda:100)
    with pytest.raises(RpcError,match='funding-rpc-unavailable'):
        await reader.transaction('missing')
    assert 'alchemy' not in reader.cooldown
    assert await reader.call('getAccountInfo',['wallet',{}])==account()
    assert await reader.call('getSignaturesForAddress',['wallet',{'limit':1}])==[]
    assert await reader.transaction('another-fund')==good
    assert len(public.calls)==1 and len(alchemy.calls)==4
    snapshot=reader.snapshot()
    assert snapshot['unavailable']==1 and snapshot['recovered']==3
    assert snapshot['public_cooldown_skips']==3
    assert snapshot['last_failure']['alchemy']['scope']=='request'
    assert 'fixture-secret' not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_min_context_slot_error_does_not_block_another_account():
    reader=FundingRpc(Client(RpcError('getAccountInfo:RPC--32016'),account()))
    with pytest.raises(RpcError):await reader.call('getAccountInfo',['new-wallet',{'minContextSlot':1001}])
    assert reader.cooldown=={}
    assert await reader.call('getAccountInfo',['other-wallet',{'minContextSlot':999}])==account()
    assert reader.snapshot()['last_failure']['public']['reason']=='RPC--32016'


@pytest.mark.asyncio
async def test_existing_endpoint_cooldown_is_not_extended_by_reader():
    priority=Priority();url='https://public.example'
    session=Session(Reply(200,{'result':parsed()}))
    public=Rpc(session,url,priority)
    reader=FundingRpc(public)
    priority.cooldown[url]=time.monotonic()+60
    with pytest.raises(RpcError):await reader.transaction('fund')
    assert session.calls==0 and reader.cooldown=={}
    assert reader.snapshot()['public_cooldown_skips']==1
    # Underlying deadline ends; no additional 30-second reader penalty remains.
    priority.cooldown.clear()
    assert await reader.transaction('fund')==session.reply.body['result']
    assert session.calls==1


@pytest.mark.asyncio
@pytest.mark.parametrize('error',['HTTP-429','HTTP-503','HTTP-403','getTransaction:TimeoutError',
                                 'getTransaction:RPC--32005','getTransaction:RPC-429'])
async def test_real_provider_failure_retains_bounded_cooldown(error):
    reader=FundingRpc(Client(RpcError(error)),clock=lambda:100)
    with pytest.raises(RpcError):await reader.transaction('fund')
    assert reader.cooldown['public']==130
    with pytest.raises(RpcError):await reader.transaction('other-fund')
    assert len(reader.public.calls)==1
    assert reader.snapshot()['public_errors']==1
    assert reader.snapshot()['public_cooldown_skips']==1
    assert reader.snapshot()['cooldown_remaining_s']=={'public':30}


@pytest.mark.asyncio
async def test_invalid_result_does_not_disrupt_concurrent_healthy_request():
    entered=asyncio.Event();release=asyncio.Event()
    class Alchemy:
        async def call(self,method,params):
            if method=='getAccountInfo':
                entered.set();await release.wait();return account()
            return None
    reader=FundingRpc(None,Alchemy(),clock=lambda:100)
    task=asyncio.create_task(reader.call('getAccountInfo',['wallet',{}]))
    try:
        await asyncio.wait_for(entered.wait(),1)
        with pytest.raises(RpcError):await reader.transaction('not-yet-available')
        release.set()
        assert await task==account()
        assert reader.cooldown=={}
        assert await reader.call('getAccountInfo',['another-wallet',{}])==account()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_diagnostics_distinguish_result_failure_and_redact_provider_secrets(caplog):
    caplog.set_level('INFO')
    reader=FundingRpc(Client(None,None,RpcError('https://host/fixture-secret')),clock=lambda:100)
    for _ in range(3):
        with pytest.raises(RpcError):await reader.transaction('private-signature')
    assert reader.snapshot()['public_result_unavailable']==2
    assert reader.snapshot()['public_errors']==1
    logs=[r.message for r in caplog.records if 'RPC read deferred' in r.message]
    assert len(logs)==2  # Same result-null diagnostic is throttled for a minute.
    assert 'reason=result-null scope=request' in logs[0]
    assert 'reason=rpc-error scope=provider' in logs[1]
    assert 'fixture-secret' not in caplog.text and 'private-signature' not in caplog.text
