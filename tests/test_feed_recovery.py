"""Offline endpoint selection, reconnect diagnostics and stream head recovery."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import aiohttp
import pytest

from chain_common.public_rpc import rpc_notices, report_429
from chain_common.rpc import RpcError
from features.funding.discovery import Discovery
from features.funding.grpc_feed import HotlistFeed
from features.funding.stream_rpc import StreamHeadRpc
from share_common.config import load
from tests.helpers import Notices
from tests.test_funding_history_rpc import wired_service


@pytest.mark.parametrize('key,ws,override,expected',[
    ('','wss://api.mainnet-beta.solana.com','','wss://api.mainnet-beta.solana.com'),
    ('secret','wss://api.mainnet-beta.solana.com','','wss://solana-mainnet.streaming.alchemy.com/v2/secret'),
    ('secret','wss://api.mainnet.solana.com/','','wss://solana-mainnet.streaming.alchemy.com/v2/secret'),
    ('secret','wss://custom.example/path','','wss://custom.example/path'),
    ('secret','wss://api.mainnet-beta.solana.com','wss://override.example','wss://override.example'),
])
def test_funding_websocket_selection_preserves_execution(config,key,ws,override,expected):
    cfg=replace(config,alchemy_key=key,ws=ws,funding_ws=override)
    assert cfg.funding_ws_url==expected
    assert cfg.ws==ws and cfg.rpc==config.rpc
    assert expected not in repr(cfg)
    assert cfg.funding_ws_provider==('alchemy' if '.alchemy.com/' in expected else 'configured')


def test_funding_websocket_override_loaded():
    cfg=load(env={'SOL_FUNDING_WS_URL':'wss://override.example','ALCHEMY_API_KEY':'fixture-key'})
    assert cfg.funding_ws_url=='wss://override.example'


class Client:
    def __init__(self,result):self.result=result;self.calls=[]
    async def call(self,method,params):
        self.calls.append((method,params))
        if isinstance(self.result,BaseException):raise self.result
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[RpcError('HTTP-429'),RpcError('endpoint-cooling'),None,True,-1,{},'1000'])
async def test_head_fallback_validates_result_and_preserves_live_barrier(config,store,bad):
    public=Client(bad);alchemy=Client(1000)
    reader=StreamHeadRpc(public,alchemy)
    feed=HotlistFeed(config,store,reader,Notices(),None)
    feed.last_slot=1100
    assert await feed.resume_slot({'wallet'}) is None
    assert feed.live_floor==1101
    assert public.calls==alchemy.calls==[('getSlot',[{'commitment':config.hotlist_commitment}])]
    assert reader.snapshot()['recovered']==1


@pytest.mark.asyncio
async def test_head_healthy_public_avoids_paid_call():
    public=Client(1000);alchemy=Client(1001)
    assert await StreamHeadRpc(public,alchemy).call('getSlot',[])==1000
    assert alchemy.calls==[]


@pytest.mark.asyncio
async def test_head_cancellation_does_not_fallback():
    alchemy=Client(1000)
    with pytest.raises(asyncio.CancelledError):
        await StreamHeadRpc(Client(asyncio.CancelledError()),alchemy).call('getSlot',[])
    assert alchemy.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered',[False,True])
async def test_head_429_is_local_and_only_combined_failure_alerts(config,store,monkeypatch,recovered):
    class Limited(Client):
        async def call(self,method,params):
            report_429('background',method,'HTTP')
            raise RpcError('HTTP-429')
    reader=StreamHeadRpc(Limited(None),Client(1000 if recovered else RpcError('HTTP-500')))
    notices=Notices();feed=HotlistFeed(config,store,reader,notices,None)
    if recovered:
        with rpc_notices(notices):await feed.resume_slot({'wallet'})
        assert not [args for args,kw in notices.items if kw.get('alert')]
    else:
        monkeypatch.setattr(feed,'addresses',lambda:{'wallet'})
        async def stop(*args):raise asyncio.CancelledError
        monkeypatch.setattr('features.funding.grpc_feed.asyncio.sleep',stop)
        with rpc_notices(notices),pytest.raises(asyncio.CancelledError):await feed.run()
        alerts=[args for args,kw in notices.items if kw.get('alert')]
        assert alerts==[]  # A short outage remains local until it exceeds 60s.
        assert notices.items[-1][0][1]['stage']=='getSlot'
        assert reader.snapshot()['unavailable']==1


@pytest.mark.asyncio
async def test_runtime_head_uses_alchemy_during_shared_public_cooldown(config,store,monkeypatch):
    import time
    service,_,calls,reply=await wired_service(config,store,monkeypatch,'fixture-key')
    service.priority.cooldown[service.config.rpc]=time.monotonic()+60
    async def result(self):return {'result':1000}
    monkeypatch.setattr(reply,'json',result)
    assert await service.stream_head_rpc.call('getSlot',[])==1000
    assert calls[-1][0]==service.config.funding_history_url  # Shared 429 bypasses public in every lane.
    service.stream_head_rpc.public.priority.cooldown[service.config.rpc]=time.monotonic()+60
    assert await service.stream_head_rpc.call('getSlot',[])==1000
    assert calls[-1][0]==service.config.funding_history_url


def socket_session(monkeypatch,socket,urls):
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,url,**kwargs):urls.append(url);return socket
    monkeypatch.setattr('features.funding.discovery.aiohttp.ClientSession',lambda **kw:Session())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['close','reject','handshake'])
async def test_funding_socket_uses_alchemy_and_redacts_errors(config,store,monkeypatch,failure):
    cfg=replace(config,alchemy_key='fixture-secret');notices=Notices();urls=[]
    class Socket:
        close_code=1008
        async def __aenter__(self):
            if failure=='handshake':raise aiohttp.WSServerHandshakeError(None,(),status=403,message='fixture-secret')
            return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):pass
        async def receive(self):
            if failure=='reject':
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(
                    {'id':1,'error':{'code':-32005,'message':'fixture-secret'}}))
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE,data=1008,extra='fixture-secret')
    socket_session(monkeypatch,Socket(),urls)
    async def stop(*args):raise asyncio.CancelledError
    monkeypatch.setattr('features.funding.discovery.asyncio.sleep',stop)
    discovery=Discovery(cfg,store,None,notices)
    with pytest.raises(asyncio.CancelledError):await discovery.websocket()
    assert urls==[cfg.funding_ws_url]
    detail=notices.items[-1][0][1]
    assert detail['provider']=='alchemy'
    assert detail['reason']=={'close':'websocket-closed','reject':'subscription-rejected','handshake':'websocket-handshake'}[failure]
    assert 'fixture-secret' not in str(notices.items)
    assert not discovery.connected


@pytest.mark.asyncio
async def test_idle_polling_keeps_single_receive_and_bounds_subscription_burst(config,store,monkeypatch):
    notices=Notices();urls=[];sent=[];reads=[];cancelled=[];polls=[]
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):sent.append(body)
        async def receive(self):
            reads.append(True)
            try:await asyncio.Future()
            finally:cancelled.append(True)
    socket_session(monkeypatch,Socket(),urls)
    async def poll(tasks,timeout,**kwargs):
        polls.append(next(t for t in tasks if t.get_coro().__name__=="receive"))
        await asyncio.sleep(0)
        if len(polls)==3:raise asyncio.CancelledError
        return set(),tasks
    monkeypatch.setattr('features.funding.discovery.asyncio.wait',poll)
    discovery=Discovery(config,store,None,notices)
    discovery.addresses=[f'wallet-{i}' for i in range(28)]
    monkeypatch.setattr(discovery,'refresh',lambda:None)
    with pytest.raises(asyncio.CancelledError):await discovery.websocket()
    assert len(sent)==8 and len(reads)==1 and len(cancelled)==1
    assert len(set(polls))==1
    assert not discovery.connected  # Socket handshake alone is not subscribed coverage.


@pytest.mark.asyncio
async def test_subscription_batches_complete_and_live_notifications_are_durable(config,store,monkeypatch):
    replies=asyncio.Queue();sent=[];urls=[]
    discovery=Discovery(config,store,None,Notices())
    acknowledgements=[];discovery.market_subscribed=acknowledgements.append
    discovery.addresses=[f'wallet-{i}' for i in range(28)]
    monkeypatch.setattr(discovery,'refresh',lambda:None)
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):
            sent.append(body)
            await replies.put({'id':body['id'],'result':body['id']+100})
        async def receive(self):
            if replies.empty() and len(sent)==28:
                assert discovery.connected and len(discovery.subscribed)==28
                if discovery.received:raise asyncio.CancelledError
                body={'params':{'subscription':101,'result':{'value':{'signature':'live-notification','err':None}}}}
            else:body=await replies.get()
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(body))
    socket_session(monkeypatch,Socket(),urls)
    with pytest.raises(asyncio.CancelledError):await discovery.websocket()
    assert store.is_live('live-notification',15)
    assert store.rows('funding','SELECT signature FROM jobs')==[{'signature':'live-notification'}]
    assert len(sent)==28
    assert set(acknowledgements)==set(discovery.addresses) and len(acknowledgements)==28


@pytest.mark.asyncio
async def test_missing_subscription_ack_reconnects_instead_of_false_healthy(config,store,monkeypatch):
    import time
    from features.funding import discovery as module
    clock=[100];notices=Notices()
    monkeypatch.setattr(module,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0]))
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):pass
        async def receive(self):await asyncio.Future()
    socket_session(monkeypatch,Socket(),[])
    async def poll(tasks,timeout,**kwargs):
        clock[0]+=16
        return set(),tasks
    async def stop(*args):raise asyncio.CancelledError
    monkeypatch.setattr(module.asyncio,'wait',poll)
    monkeypatch.setattr(module.asyncio,'sleep',stop)
    discovery=Discovery(config,store,None,notices)
    with pytest.raises(asyncio.CancelledError):await discovery.websocket()
    assert notices.items[-1][0][1]['reason']=='subscription-ack-timeout'
    assert not discovery.connected


@pytest.mark.asyncio
async def test_large_backup_subscription_sorts_once_not_for_every_ack(config,store,monkeypatch):
    import time
    from features.funding import discovery as module
    from features.funding.create_feed import CreateFeed
    replies=asyncio.Queue();sent=[];sorts=[]
    feed=CreateFeed(config,store,None,Notices())
    feed.addresses=[f'wallet-{i}' for i in range(5250)]
    monkeypatch.setattr(feed,'refresh',lambda:None)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=time.time,monotonic=lambda:100))
    def ordered(values):
        result=sorted(values);sorts.append(len(result));return result
    monkeypatch.setattr(module,'sorted',ordered,raising=False)
    def forbidden(*args):raise AssertionError('ACK must not load the hotlist')
    monkeypatch.setattr(store,'hotlist',forbidden)
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):
            sent.append(body)
            await replies.put({'id':body['id'],'result':body['id']+100})
        async def receive(self):
            if replies.empty() and len(sent)==len(feed.addresses):
                assert feed.connected and feed.subscribed==set(feed.addresses)
                raise asyncio.CancelledError
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(await replies.get()))
    socket_session(monkeypatch,Socket(),[])
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(feed.websocket(),10)
    assert len(sent)==5250 and sorts==[5250]
    assert feed.received==0


@pytest.mark.asyncio
async def test_subscription_refresh_removes_old_and_adds_new_without_stale_coverage(config,store,monkeypatch):
    import time
    from features.funding import discovery as module
    replies=asyncio.Queue();sent=[];clock=[100];desired=[['a','b']]
    feed=Discovery(config,store,None,Notices())
    monkeypatch.setattr(feed,'refresh',lambda:setattr(feed,'addresses',desired[0]))
    monkeypatch.setattr(module,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0]))
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):
            sent.append(body)
            await replies.put({'id':body['id'],'result':body['id']+100 if body['method']=='logsSubscribe' else True})
        async def receive(self):
            if replies.empty() and len(sent)==4:
                assert feed.connected and feed.subscribed=={'b','c'}
                raise asyncio.CancelledError
            body=await replies.get()
            if body['id']==2:desired[0]=['b','c'];clock[0]=103
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(body))
    socket_session(monkeypatch,Socket(),[])
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(feed.websocket(),2)
    assert [row['method'] for row in sent]==['logsSubscribe','logsSubscribe','logsUnsubscribe','logsSubscribe']
    assert sent[2]['params']==[101] and sent[3]['params'][0]['mentions']==['c']


def test_alchemy_socket_rate_limit_label():
    notices=Notices()
    with rpc_notices(notices):report_429('alchemy-websocket','subscription','WS-RPC')
    assert notices.items[0][0][0]=='Alchemy RPC 429'
    assert notices.items[0][0][1]['source']=='alchemy-websocket'
