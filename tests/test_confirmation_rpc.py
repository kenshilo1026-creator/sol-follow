"""Finality/status recovery preserves evidence and alerts only after both fail."""
import asyncio
import time

import pytest

from chain_common.public_rpc import rpc_notices
from chain_common.rpc import Priority, Rpc, RpcError
from features.funding.confirmation_rpc import ConfirmationRpc, ConfirmationUnavailable
from features.funding.processed import Processed
from features.runtime.service import Service
from share_common.notify import Notices, message
from tests.test_funding_rpc_recovery import config, store, Client, parsed
from tests.test_funding_rpc_alerts import queued
from tests.test_rpc_alerts import Reply, Session


PARAMS=[['fund'],{'searchTransactionHistory':True}]


def status(state='finalized',err=None,slot=1000):
    return {'slot':slot,'confirmations':None,'err':err,'confirmationStatus':state}


def response(*rows):
    return {'context':{'slot':2000},'value':list(rows)}


def reader_with_replies(public_reply,alchemy_reply):
    priority=Priority()
    public=Session(public_reply); alchemy=Session(alchemy_reply)
    reader=ConfirmationRpc(Rpc(public,'https://public.example',priority,source='background'),
        Rpc(alchemy,'https://alchemy.example/fixture-secret',priority,source='alchemy-history'))
    return reader,public,alchemy


@pytest.mark.asyncio
async def test_healthy_public_preserves_mixed_statuses_without_alchemy():
    value=response(status(),None,status('processed'),status('confirmed',err={'InstructionError':[0,'bad']}))
    public,alchemy=Client(value),Client()
    assert await ConfirmationRpc(public,alchemy).call('getSignatureStatuses',[
        ['a','b','c','d'],{'searchTransactionHistory':True}]) is value
    assert alchemy.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[
    RpcError('HTTP-429'),RpcError('endpoint-cooling'),RpcError('getSignatureStatuses:TimeoutError'),
    None,{}, {'value':[status()]},response(),response(status(),status()),
    {'context':{'slot':True},'value':[status()]},response({'confirmationStatus':'finalized'}),
    response({**status(),'slot':True}),response({**status(),'slot':-1}),
    response({**status(),'slot':2001}),response({**status(),'confirmationStatus':'wrong'}),
    response({**status(),'err':False}),
])
async def test_failed_or_malformed_public_uses_same_batch_on_alchemy(bad):
    good=response(status());public,alchemy=Client(bad),Client(good)
    reader=ConfirmationRpc(public,alchemy)
    assert await reader.call('getSignatureStatuses',PARAMS) is good
    assert public.calls==alchemy.calls==[('getSignatureStatuses',PARAMS)]
    assert reader.snapshot()['recovered']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('code,body',[(429,None),(200,{'error':{'code':429}})])
async def test_recovered_429_is_local_only_including_followup_cooldown(config,store,code,body):
    reader,public,alchemy=reader_with_replies(Reply(code,body),Reply(200,{'result':response(status())}))
    notices=Notices(config,store)
    with rpc_notices(notices):
        for _ in range(2):
            assert (await reader.call('getSignatureStatuses',PARAMS))['value'][0]['confirmationStatus']=='finalized'
    assert public.calls==1 and alchemy.calls==2
    rows=queued(notices)
    assert len(rows)==1 and rows[0][0]=='public RPC 429' and not rows[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[None,response(),response({**status(),'err':False})])
async def test_both_unavailable_alerts_once_and_never_invents_results(config,store,value):
    reader,public,alchemy=reader_with_replies(Reply(),Reply(200,{'result':value}))
    now=[0.0]
    reader.clock=lambda:now[0]
    notices=Notices(config,store)
    with rpc_notices(notices):
        for stamp in (0,61):
            now[0]=stamp
            with pytest.raises(ConfirmationUnavailable,match='^confirmation-rpc-unavailable$'):
                await reader.call('getSignatureStatuses',PARAMS)
    assert public.calls==1 and alchemy.calls==2
    alerts=[(kind,detail) for kind,detail,alert in queued(notices) if alert]
    assert len(alerts)==1 and alerts[0][0]=='confirmation RPC unavailable'
    assert 'Alchemy' in message(*alerts[0]) and 'fixture-secret' not in message(*alerts[0])


@pytest.mark.asyncio
async def test_unrelated_methods_never_use_status_fallback():
    alchemy=Client()
    with pytest.raises(RpcError):
        await ConfirmationRpc(Client(RpcError('HTTP-429')),alchemy).call('sendTransaction',['raw'])
    assert alchemy.calls==[]


@pytest.mark.asyncio
async def test_cancellation_does_not_alert_or_attempt_another_provider(config,store):
    notices=Notices(config,store);alchemy=Client()
    with rpc_notices(notices):
        with pytest.raises(asyncio.CancelledError):
            await ConfirmationRpc(Client(asyncio.CancelledError()),alchemy).call('getSignatureStatuses',PARAMS)
    assert alchemy.calls==[] and notices.queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['','fixture-secret'])
async def test_runtime_status_reader_isolates_budgets_and_funding_failure_state(config,store,monkeypatch,key):
    from tests.test_funding_history_rpc import wired_service
    service,_,calls,reply=await wired_service(config,store,monkeypatch,key)
    reader=service.confirmation_rpc
    assert reader.public is not service.background_rpc
    assert reader.public.priority is not service.priority
    assert bool(reader.alchemy)==bool(key)
    assert reader is not service.funding_rpc
    service.funding_rpc.cooldown['public']=time.monotonic()+60
    async def json_result(self):return {'result':response(status())}
    monkeypatch.setattr(reply,'json',json_result)
    assert await reader.call('getSignatureStatuses',PARAMS)==response(status())
    assert calls[-1][0]==service.config.rpc  # Funding validation failures do not stall status reads.
    service.priority.cooldown[service.config.rpc]=time.monotonic()+60
    # Unlike per-reader failures above, a real endpoint limit is shared.
    assert reader.public.priority.cooldown is service.priority.cooldown
    if key:
        assert await reader.call('getSignatureStatuses',PARAMS)==response(status())
        assert calls[-1][0]==service.config.funding_history_url
    else:
        with pytest.raises(RpcError):await reader.call('getSignatureStatuses',PARAMS)


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered',[False,True])
async def test_finalization_recovery_or_single_outage_alert_preserves_chain_checks(config,store,monkeypatch,recovered):
    service=Service(config,store)
    with store.db('funding') as db:
        db.execute('INSERT INTO chain_checks VALUES (?,?,?)',('fund',1000,time.time()))
    fallback=Reply(200,{'result':response(status())}) if recovered else Reply()
    reader,_,_=reader_with_replies(Reply(),fallback)
    async def stop(_):raise asyncio.CancelledError()
    monkeypatch.setattr('features.runtime.service.asyncio.sleep',stop)
    with rpc_notices(service.notices):
        with pytest.raises(asyncio.CancelledError):await service.finalized(reader)
    assert bool(store.rows('funding','SELECT * FROM chain_checks')) is (not recovered)
    alerts=[kind for kind,_,alert in queued(service.notices) if alert]
    assert alerts==[]  # Transient combined failure is local until >60s.


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['confirmed','failed','different-slot','missing','unavailable'])
async def test_processed_recovery_keeps_existing_confirmation_and_revocation_rules(config,store,mode):
    raw=parsed();raw['_stream']={'commitment':'processed','live':True}
    store.stream_transaction(raw)
    with store.db('funding') as db:
        db.execute('UPDATE processed_signals SET seen=?',(time.time()-31,))
    value=status('confirmed')
    if mode=='failed':value['err']={'InstructionError':[0,'bad']}
    elif mode=='different-slot':value['slot']+=1
    elif mode=='missing':value=None
    fallback=Reply(500) if mode=='unavailable' else Reply(200,{'result':response(value)})
    reader,_,_=reader_with_replies(Reply(),fallback)
    service=Service(config,store);proof=Processed(store)
    with rpc_notices(service.notices):
        if mode=='unavailable':
            with pytest.raises(ConfirmationUnavailable):
                await proof.reconcile_once(reader,service.signals,service.notices,3600)
        else:
            await proof.reconcile_once(reader,service.signals,service.notices,3600)
    expected={'confirmed':'confirmed','failed':'invalid','different-slot':'invalid',
              'missing':'uncertain','unavailable':'pending'}[mode]
    assert proof.row('fund')['state']==expected
    assert proof.usable('fund')==(mode=='confirmed')
    alerts=[kind for kind,_,alert in queued(service.notices) if alert]
    if mode=='unavailable':assert alerts==[]
    elif mode in ('confirmed','missing'):assert alerts==[]
