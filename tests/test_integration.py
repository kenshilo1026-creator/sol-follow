"""Offline service integration; unsupported trades never create orders."""
import asyncio
from dataclasses import replace
import json
import time
import pytest
from concurrent.futures import ThreadPoolExecutor
from chain_common.primitives import SYSTEM
from features.runtime.service import Service
from features.funding.discovery import Discovery
from launchpads import pump_fun
from tests.helpers import address,transaction,Notices


def test_concurrent_reservation(config,store):
    mint,p=address(),address()
    with ThreadPoolExecutor(max_workers=4) as workers:
        result=list(workers.map(lambda _:store.reserve(config,mint,p,10),range(8)))
    assert sum(r is not None for r in result)==1


@pytest.mark.asyncio
async def test_service_boot_and_cancel_with_local_rpc(config,store):
    from aiohttp import web,WSMsgType
    calls=[]
    async def http(request):
        body=await request.json();method=body['method'];calls.append(method)
        # Solana's published mainnet genesis hash, independent of the service constant.
        if method=='getGenesisHash':result='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
        elif method=='getSignaturesForAddress':result=[]
        else:raise AssertionError(method)
        return web.json_response({'jsonrpc':'2.0','id':body['id'],'result':result})
    async def websocket(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for message in ws:
            if message.type==WSMsgType.TEXT:
                body=json.loads(message.data)
                await ws.send_json({'jsonrpc':'2.0','id':body['id'],'result':1})
        return ws
    app=web.Application();app.router.add_post('/',http);app.router.add_get('/ws',websocket)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    cfg=replace(config,rpc=f'http://127.0.0.1:{port}/',
                ws=f'http://127.0.0.1:{port}/ws',cex={address():'test'},source_programs=(),privacy_pools=(),
                dry_run=False,wallet_file=config.data/'missing-wallet.json',wallet_address=address())
    oid=store.reserve(cfg,address(),address(),10)
    store.update_order(oid,state='unknown',signature='existing-signature',raw='existing-signed-bytes')
    service=Service(cfg,store)
    before=store.order(oid)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(service.run(),timeout=0.8)
        assert 'getGenesisHash' in calls and 'getSignaturesForAddress' in calls
        assert store.order(oid)==before
        assert not any(method in calls for method in ('sendTransaction','simulateTransaction','getLatestBlockhash','getBalance'))
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('genesis', [
    'EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG',  # devnet
    '4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY',  # testnet
    '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2d1',  # former typo
    None,
])
async def test_service_rejects_non_mainnet_before_starting(config, store, monkeypatch, caplog, genesis):
    from chain_common.rpc import Rpc
    calls = []

    async def call(self, method, params):
        calls.append(method)
        assert method == 'getGenesisHash'
        return genesis

    monkeypatch.setattr(Rpc, 'call', call)
    service = Service(config, store)
    with pytest.raises(ValueError, match='only-solana-mainnet-beta-is-supported'):
        await service.run()
    assert calls == ['getGenesisHash']
    assert not hasattr(service, 'discovery')
    assert not hasattr(service, 'executor')
    assert 'RPC genesis hash does not match Solana mainnet-beta' in caplog.text


@pytest.mark.asyncio
async def test_websocket_subscribes_all_addresses(config,store):
    from aiohttp import web,WSMsgType
    expected={address() for _ in range(300)}
    received=set()
    complete=asyncio.Event()
    async def websocket(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for message in ws:
            if message.type==WSMsgType.TEXT:
                body=json.loads(message.data)
                assert body['method']=='logsSubscribe'
                received.update(body['params'][0]['mentions'])
                await ws.send_json({'jsonrpc':'2.0','id':body['id'],'result':body['id']})
                if received==expected:complete.set()
        return ws
    app=web.Application();app.router.add_get('/ws',websocket)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    cfg=replace(config,ws=f'http://127.0.0.1:{port}/ws',
                cex=dict.fromkeys(expected,'test'),source_programs=(),privacy_pools=())
    discovery=Discovery(cfg,store,None,Notices())
    task=asyncio.create_task(discovery.websocket())
    try:
        await asyncio.wait_for(complete.wait(),timeout=5)
        async with asyncio.timeout(5):
            while discovery.subscribed!=expected:
                await asyncio.sleep(0.01)
        assert received==expected
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_funding_still_admitted_without_trading(config,store):
    now=int(time.time());cex,wallet=address(),address()
    cfg=replace(config,cex={cex:'test'})
    transfer={'programId':SYSTEM,'parsed':{'type':'transfer','info':{'source':cex,'destination':wallet,'lamports':10**9}}}
    raw=transaction([cex,wallet],[cex],[transfer],pre=[2*10**9,0],post=[10**9-5000,10**9],when=now,sig='funding')
    class RPC:
        async def transaction(self,sig):return raw
        async def call(self,method,params):
            if method=='getSignaturesForAddress':
                assert params[0]==wallet
                return [{'signature':'funding','slot':raw['slot'],'blockTime':now}]
            assert method=='getAccountInfo' and params[0]==wallet
            return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
    store.enqueue(['funding'])
    await Service(cfg,store).process({'signature':'funding'},RPC())
    assert wallet in store.hotlist(now)
    assert not store.rows('trading','SELECT * FROM orders')


@pytest.mark.asyncio
async def test_launchpad_transaction_does_not_vote_or_reserve(config,store):
    now=int(time.time());wallet=address()
    raw=transaction([wallet],[wallet],[{'programId':pump_fun.PROGRAM,'accounts':[],'data':''}],when=now,sig='launch')
    class RPC:
        async def transaction(self,sig):return raw
        async def call(self,*args):raise AssertionError('no trading RPC')
    store.enqueue(['launch'])
    service=Service(config,store)
    await service.process({'signature':'launch'},RPC())
    assert not store.rows('trading','SELECT * FROM votes')
    assert not store.rows('trading','SELECT * FROM orders')
    assert store.rows('funding',"SELECT state FROM jobs WHERE signature='launch'")==[{'state':'done'}]
