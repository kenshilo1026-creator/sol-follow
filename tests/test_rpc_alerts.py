import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import aiohttp
import pytest
from chain_common.rpc import Rpc, Priority, RpcError
from chain_common.public_rpc import rpc_notices, report_429, sdk_event
from share_common.notify import Notices


class Reply:
    def __init__(self,status=429,body=None):
        self.status=status;self.body=body;self.headers={'Retry-After':'1'}
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def json(self):return self.body


class Session:
    def __init__(self,reply):self.reply=reply;self.calls=0
    def post(self,*args,**kwargs):self.calls+=1;return self.reply


@pytest.mark.asyncio
@pytest.mark.parametrize('method',['getAccountInfo','getSignaturesForAddress','getTransaction','sendTransaction','getGenesisHash'])
@pytest.mark.parametrize('status,body,transport',[(429,None,'HTTP'),(200,{'error':{'code':429,'message':'secret-url'}},'JSON-RPC')])
async def test_each_real_429_alerts_without_cooldown_duplicates(config,store,method,status,body,transport):
    notices=Notices(config,store);priority=Priority();session=Session(Reply(status,body))
    rpc=Rpc(session,'https://private.example/secret-key',priority)
    with rpc_notices(notices):
        for attempt in range(2):
            priority.cooldown.clear()
            with pytest.raises(RpcError):await rpc.call(method,[])
            with pytest.raises(RpcError,match='endpoint-cooling'):await rpc.call(method,[])
    assert session.calls==rpc.limited==notices.queue.qsize()==2
    while not notices.queue.empty():
        kind,detail,alert=notices.queue.get_nowait()
        assert kind=='public RPC 429' and alert
        assert detail=={'source':'python','method':method,'transport':transport,'status':429}
        assert 'secret' not in json.dumps(detail)


@pytest.mark.asyncio
async def test_shared_background_rpc_reports_without_changing_backoff(config,store):
    from chain_common.background_rpc import BackgroundRpc
    notices=Notices(config,store);priority=Priority()
    rpc=BackgroundRpc(Rpc(Session(Reply()),'http://local',priority,source='background'),priority)
    with rpc_notices(notices):
        with pytest.raises(RpcError):await rpc.transaction('sig')
    assert notices.queue.get_nowait()[1]['source']=='background'


@pytest.mark.asyncio
async def test_unhealthy_node_is_not_misreported_as_429(config,store):
    notices=Notices(config,store)
    with rpc_notices(notices):
        rpc=Rpc(Session(Reply(200,{'error':{'code':-32005}})),'http://local',Priority())
        with pytest.raises(RpcError):await rpc.call('getBalance',[])
    assert notices.queue.empty()


@pytest.mark.asyncio
async def test_startup_429_is_sent_even_when_service_exits(config,store,monkeypatch):
    from features.runtime.service import Service
    import share_common.notify as module
    sent=[]
    class Telegram:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,json):sent.append(json);return Reply(200)
    monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:Telegram())
    cfg=replace(config,telegram_token='fake',telegram_chat='test')
    service=Service(cfg,store)
    async def startup():
        await Rpc(Session(Reply()),'http://local',Priority()).call('getGenesisHash',[])
    monkeypatch.setattr(service,'_run',startup)
    with pytest.raises(RpcError):await service.run()
    assert len(sent)==1 and '429' in sent[0]['text'] and 'getGenesisHash' in sent[0]['text']
    assert service.notices.queue.empty()


@pytest.mark.asyncio
async def test_sdk_event_does_not_consume_pending_build_response(config,store):
    from trade_execution.cache import CachedBuilder
    notices=Notices(config,store);cache=CachedBuilder(config,store,notices,Priority())
    stream=asyncio.StreamReader()
    for row in [{'event':'public-rpc-429','method':'getMultipleAccounts','transport':'HTTP'},
                {'id':7,'result':{'warmed':True}}]:
        stream.feed_data((json.dumps(row)+'\n').encode())
    stream.feed_eof()
    async def wait():return 0
    process=SimpleNamespace(stdout=stream,returncode=0,wait=wait)
    future=asyncio.get_running_loop().create_future();cache.pending[7]=future
    with rpc_notices(notices):await cache.read(process)
    assert future.result()['result']=={'warmed':True}
    assert notices.queue.get_nowait()[1]['source']=='node-sdk'


@pytest.mark.asyncio
async def test_websocket_handshake_429_reports(config,store,monkeypatch):
    from features.funding.discovery import Discovery
    import features.funding.discovery as module
    notices=Notices(config,store)
    class SocketSession:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,*args,**kwargs):raise aiohttp.WSServerHandshakeError(None,(),status=429)
    async def stop(*args):raise asyncio.CancelledError
    monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:SocketSession())
    monkeypatch.setattr(module.asyncio,'sleep',stop)
    with rpc_notices(notices):
        with pytest.raises(asyncio.CancelledError):await Discovery(config,store,None,notices).websocket()
    kind,detail,alert=notices.queue.get_nowait()
    assert kind=='public RPC 429' and detail['transport']=='WS-handshake' and alert


def test_ipc_never_forwards_urls_or_provider_error_text(config,store):
    notices=Notices(config,store)
    with rpc_notices(notices):
        assert sdk_event({'event':'public-rpc-429','method':'https://host/secret','transport':'secret'})
        assert not sdk_event({'id':1,'result':{}})
    detail=notices.queue.get_nowait()[1]
    assert detail['method']==detail['transport']=='unknown'


@pytest.mark.asyncio
async def test_one_shot_sdk_alert_arrives_before_build_finishes(config,store,monkeypatch):
    from trade_execution import builder
    notices=Notices(config,store)
    class Input:
        def write(self,data):pass
        async def drain(self):pass
        def close(self):pass
    class Process:
        returncode=None
        def __init__(self):
            self.stdin=Input();self.stdout=asyncio.StreamReader();self.stderr=asyncio.StreamReader()
            self.stderr.feed_data(b'{"event":"public-rpc-429","method":"simulateTransaction","transport":"HTTP"}\n')
            self.stderr.feed_eof();self.finished=asyncio.Event()
        async def wait(self):await self.finished.wait();return self.returncode
        def finish(self):
            self.returncode=0;self.stdout.feed_data(b'{"ok":true}');self.stdout.feed_eof();self.finished.set()
        def kill(self):self.finish()
    process=Process()
    async def spawn(*args,**kwargs):return process
    monkeypatch.setattr(builder.asyncio,'create_subprocess_exec',spawn)
    route=SimpleNamespace(request=lambda:{'route':'pump_native_curve'})
    with rpc_notices(notices):
        task=asyncio.create_task(builder._build(config,route,'wallet'))
        try:
            kind,detail,alert=await asyncio.wait_for(notices.queue.get(),1)
            assert kind=='public RPC 429' and detail['method']=='simulateTransaction' and alert
            assert not task.done()
        finally:
            process.finish()
            assert await task=={'ok':True}


@pytest.mark.asyncio
async def test_websocket_json_rpc_429_reports(config,store,monkeypatch):
    from features.funding.discovery import Discovery
    import features.funding.discovery as module
    notices=Notices(config,store)
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,*args):pass
        async def receive(self):
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data='{"id":1,"error":{"code":429}}')
    class SocketSession:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,*args,**kwargs):return Socket()
    async def stop(*args):raise asyncio.CancelledError
    monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:SocketSession())
    monkeypatch.setattr(module.asyncio,'sleep',stop)
    discovery=Discovery(config,store,None,notices)
    discovery.addresses=['wallet']
    monkeypatch.setattr(discovery,'refresh',lambda:None)
    with rpc_notices(notices):
        with pytest.raises(asyncio.CancelledError):await discovery.websocket()
    kind,detail,alert=notices.queue.get_nowait()
    assert kind=='public RPC 429' and detail['transport']=='WS-RPC' and alert
