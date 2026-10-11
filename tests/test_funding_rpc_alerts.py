"""Notify on failed funding recovery, not on a recovered provider 429."""
import asyncio
import json

import pytest

from chain_common.public_rpc import rpc_notices
from chain_common.rpc import Priority, Rpc, RpcError
from features.funding.rpc import FundingRpc
from share_common.notify import Notices, message
from tests.test_funding_rpc_recovery import config, store, parsed, Client
from tests.test_rpc_alerts import Reply, Session


def clients(public_reply,alchemy_reply):
    priority=Priority()
    public=Session(public_reply); alchemy=Session(alchemy_reply)
    reader=FundingRpc(Rpc(public,'https://public.example',priority,source='background'),
                      Rpc(alchemy,'https://alchemy.example/fixture-secret',priority,source='alchemy-history'))
    return reader,public,alchemy


def queued(notices):
    rows=[]
    while not notices.queue.empty():
        rows.append(notices.queue.get_nowait())
        notices.queue.task_done()
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body',[(429,None),(200,{'error':{'code':429}})])
async def test_recovered_429_and_cooldown_reads_do_not_alert(config,store,status,body):
    value=parsed()
    reader,public,alchemy=clients(Reply(status,body),Reply(200,{'result':value}))
    notices=Notices(config,store)
    with rpc_notices(notices):
        assert await reader.transaction('fund')==value
        assert await reader.transaction('fund')==value
    assert public.calls==1 and alchemy.calls==2
    rows=queued(notices)
    assert len(rows)==1 and rows[0][0]=='public RPC 429'
    assert rows[0][2] is False  # Still queued for local audit.


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body',[
    (429,None),(500,None),(200,{'error':{'code':429}}),
    (200,{'result':None}),(200,{'result':{'slot':1000}}),
])
async def test_failed_recovery_sends_one_combined_alert(config,store,status,body):
    reader,public,alchemy=clients(Reply(),Reply(status,body))
    notices=Notices(config,store)
    with rpc_notices(notices):
        for _ in range(2):
            with pytest.raises(RpcError,match='funding-rpc-unavailable'):
                await reader.transaction('fund')
    assert public.calls==alchemy.calls==1
    rows=queued(notices)
    alerts=[(kind,detail) for kind,detail,alert in rows if alert]
    assert alerts==[('funding RPC unavailable',{
        'method':'getTransaction','reason':'public-and-alchemy-unavailable'})]
    assert all(not alert for kind,_,alert in rows if kind.endswith('429'))
    text=message(*alerts[0])
    assert 'getTransaction' in text and 'Alchemy' in text
    assert 'fixture-secret' not in text+json.dumps(rows)


@pytest.mark.asyncio
async def test_success_never_reaches_telegram_but_both_fail_does(config,store,monkeypatch):
    from dataclasses import replace
    sent=[]
    class Telegram:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,json):sent.append(json['text']);return Reply(200)
    monkeypatch.setattr('share_common.notify.aiohttp.ClientSession',lambda **kwargs:Telegram())
    notices=Notices(replace(config,telegram_token='fake',telegram_chat='test'),store)
    recovered,_,_=clients(Reply(),Reply(200,{'result':parsed()}))
    failed,_,_=clients(Reply(),Reply(500))
    async with notices.running():
        await recovered.transaction('fund')
        await asyncio.wait_for(notices.queue.join(),1)
        assert sent==[]
        with pytest.raises(RpcError):await failed.transaction('fund')
    assert len(sent)==1 and 'getTransaction' in sent[0] and 'Alchemy' in sent[0]
    audits=store.rows('audit','SELECT kind FROM audit')
    assert [r['kind'] for r in audits].count('public RPC 429')==2
    assert [r['kind'] for r in audits].count('funding RPC unavailable')==1


@pytest.mark.asyncio
async def test_other_tasks_keep_alerts_during_recovery(config,store):
    started=asyncio.Event(); release=asyncio.Event()
    class Recovery:
        async def call(self,*args):
            started.set()
            await release.wait()
            return parsed()
    notices=Notices(config,store)
    reader=FundingRpc(Rpc(Session(Reply()),'https://public.example',Priority()),Recovery())
    independent=Rpc(Session(Reply()),'https://other.example',Priority())
    with rpc_notices(notices):
        task=asyncio.create_task(reader.transaction('fund'))
        try:
            await asyncio.wait_for(started.wait(),1)
            with pytest.raises(RpcError):await independent.call('getSignatureStatuses',[['sig']])
            release.set()
            await asyncio.wait_for(task,1)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert [(detail['method'],alert) for _,detail,alert in queued(notices)]==[
        ('getTransaction',False),('getSignatureStatuses',True)]


@pytest.mark.asyncio
async def test_cancellation_restores_alerts_without_reporting_both_failed(config,store):
    notices=Notices(config,store)
    reader=FundingRpc(Client(asyncio.CancelledError()),Client())
    with rpc_notices(notices):
        with pytest.raises(asyncio.CancelledError):await reader.transaction('fund')
        with pytest.raises(RpcError):
            await Rpc(Session(Reply()),'https://public.example',Priority()).call('getBalance',[])
    rows=queued(notices)
    assert len(rows)==1 and rows[0][0]=='public RPC 429' and rows[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback',[False,True])
async def test_calls_without_recovery_keep_existing_alerts(config,store,fallback):
    notices=Notices(config,store)
    reader=FundingRpc(Rpc(Session(Reply()),'https://public.example',Priority()),Client() if fallback else None)
    with rpc_notices(notices):
        with pytest.raises(RpcError):
            await reader.call('getSignatureStatuses' if fallback else 'getTransaction',['sig'])
    rows=queued(notices)
    assert len(rows)==1 and rows[0][0]=='public RPC 429' and rows[0][2]
