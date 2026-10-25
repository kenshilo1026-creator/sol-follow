"""Alchemy-first source pagination and public-first execution both have fallback."""
from dataclasses import replace
import json
import time

import pytest

from chain_common.public_rpc import rpc_notices
from chain_common.rpc import RpcError
from features.funding.discovery import Discovery
from features.runtime.service import Service
from share_common.config import load
from share_common.notify import message
from tests.helpers import address
from trade_execution.builder import payload_for


@pytest.mark.parametrize('env,key',[
    ({},''),
    ({'ALCHEMY_API_KEY':'primary'},'primary'),
    ({'SOL_ALCHEMY_API_KEY':'alias'},'alias'),
    ({'ALCHEMY_API_KEY':'primary','SOL_ALCHEMY_API_KEY':'alias'},'primary'),
])
def test_history_provider_uses_local_key_without_changing_execution(tmp_path,env,key):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    cfg=load(tmp_path,env={**env,'SOL_FEED_MODE':'websocket','SOL_RPC_HTTP_URL':'https://public.example'})
    assert cfg.rpc=='https://public.example'
    assert cfg.funding_history_provider==('alchemy' if key else 'configured-public')
    assert cfg.funding_history_url==('https://solana-mainnet.g.alchemy.com/v2/'+key if key else cfg.rpc)
    assert cfg.http_policy==('alchemy-source-history-public-funding-alchemy-fallback' if key else 'configured-public-only')
    payload=payload_for(cfg,{},address())
    assert payload['rpc']==cfg.rpc
    assert payload['rpcFallback']==(cfg.funding_history_url if key else '')
    if key:
        assert key not in repr(cfg)


def test_offline_check_reports_provider_without_credentials(tmp_path,monkeypatch,capsys):
    from features import app
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    (tmp_path/'take_profit_rules.json').write_text(json.dumps({
        'gain_definition':'profit_over_entry_percent','sell_basis':'current_position','rules':[]}))
    cfg=load(tmp_path,env={'ALCHEMY_API_KEY':'fixture-secret'})
    monkeypatch.setattr(app,'load',lambda:cfg)
    monkeypatch.setattr('sys.argv',['features.app','check'])
    app.main()
    output=capsys.readouterr().out
    assert json.loads(output)['funding_history_provider']=='alchemy'
    assert json.loads(output)['confirmation_alchemy_fallback'] is True
    assert 'fixture-secret' not in output and cfg.funding_history_url not in output


async def wired_service(config,store,monkeypatch,key):
    """Exercise runtime client selection with in-memory HTTP responses only."""
    cfg=replace(config,alchemy_key=key,feed_mode='alchemy_grpc' if key else 'websocket',
                rpc='https://public.example',privacy_pools=(address(),))
    calls=[];captured=[]
    class Response:
        status=200
        headers={'Retry-After':'30'}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def json(self):return {'result':self.result}
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,json,**kwargs):
            calls.append((url,json))
            reply=Response()
            reply.result=('5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
                          if json['method']=='getGenesisHash' else
                          [dict(signature='gap-funding',slot=100,err=None,blockTime=int(time.time()))])
            return reply
    def discovery(*args,**kwargs):
        captured.append(Discovery(*args,**kwargs))
        raise RuntimeError('routing-captured')
    monkeypatch.setattr('features.runtime.service.aiohttp.ClientSession',lambda **kwargs:Session())
    monkeypatch.setattr('features.runtime.service.Discovery',discovery)
    service=Service(cfg,store)
    with pytest.raises(RuntimeError,match='routing-captured'):
        await service._run()
    return service,captured[0],calls,Response


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['','fixture-secret'])
@pytest.mark.parametrize('source_kind',['cex','privacy'])
async def test_discovery_routing_keeps_details_public_and_history_nonlive(config,store,monkeypatch,key,source_kind):
    service,discovery,calls,_=await wired_service(config,store,monkeypatch,key)
    cfg=service.config
    assert discovery.rpc is service.source_history_rpc
    assert discovery.rpc.public is service.background_rpc
    assert (discovery.rpc.alchemy is service.funding_history_rpc)==bool(key)
    assert discovery.sources_only==bool(key)
    source=next(iter(cfg.cex)) if source_kind=='cex' else cfg.privacy_pools[0]
    await discovery.page(source)
    assert calls[-1][0]==cfg.funding_history_url
    assert calls[-1][1]['method']=='getSignaturesForAddress'
    assert calls[-1][1]['params']==[source,{'limit':100,'commitment':'confirmed'}]
    assert store.rows('funding','SELECT signature FROM jobs')==[{'signature':'gap-funding'}]
    assert not store.is_live('gap-funding',15)
    prior=len(calls)
    await discovery.page(address())
    assert len(calls)==prior  # Never scan unrelated/hotlist wallets.
    await service.background_rpc.transaction('gap-funding')
    assert calls[-1][0]==cfg.rpc and calls[-1][1]['method']=='getTransaction'
    assert all(url==cfg.rpc for url,body in calls if body['method']!='getSignaturesForAddress')


@pytest.mark.asyncio
async def test_both_history_providers_fail_retains_request_and_keeps_alert_local(config,store,monkeypatch):
    service,discovery,calls,response=await wired_service(config,store,monkeypatch,'fixture-secret')
    source=next(iter(service.config.cex))
    discovery.request_history(source)
    response.status=429
    with rpc_notices(service.notices):
        with pytest.raises(RpcError,match='funding-rpc-unavailable'):
            await discovery.page(source)
    assert len(calls)==3  # Public genesis, Alchemy history, public fallback.
    assert calls[-2][0]==service.config.funding_history_url
    assert calls[-1][0]==service.config.rpc
    assert store.rows('funding','SELECT address FROM history_requests')==[{'address':source}]
    assert store.cursor(source)=={}
    assert store.rows('funding','SELECT * FROM jobs')==[]
    kind,detail,alert=service.notices.queue.get_nowait()
    assert kind=='Alchemy RPC 429' and not alert and detail['source']=='alchemy-history'
    assert 'fixture-secret' not in message(kind,detail)
    assert 'Alchemy RPC 429' in message(kind,detail)
    assert service.config.rpc in service.execution_rpc.providers[0].priority.cooldown
    response.status=200
    prior=len(calls)
    with pytest.raises(RpcError,match='funding-rpc-unavailable'):
        await discovery.page(source)
    assert len(calls)==prior
    service.priority.cooldown.clear()
    service.source_history_rpc.cooldown.clear()
    await discovery.page(source)
    assert store.rows('funding','SELECT signature FROM jobs')==[{'signature':'gap-funding'}]


@pytest.mark.asyncio
async def test_public_cooldown_does_not_block_alchemy_history(config,store,monkeypatch):
    service,discovery,calls,_=await wired_service(config,store,monkeypatch,'fixture-secret')
    service.priority.cooldown[service.config.rpc]=time.monotonic()+30
    await discovery.page(next(iter(service.config.cex)))
    assert calls[-1][0]==service.config.funding_history_url


@pytest.mark.asyncio
async def test_source_history_alchemy_failure_recovers_on_public_without_live_buy(config,store,monkeypatch):
    from chain_common.public_rpc import report_429
    service,discovery,_,_=await wired_service(config,store,monkeypatch,'fixture-secret')
    attempts=[]
    async def limited(method,params):
        attempts.append('alchemy')
        report_429('alchemy-history',method,'HTTP')
        raise RpcError('HTTP-429')
    async def healthy(method,params):
        attempts.append('public')
        return [{'signature':'funding-gap','slot':100,'err':None,'blockTime':int(time.time())}]
    monkeypatch.setattr(service.funding_history_rpc,'call',limited)
    monkeypatch.setattr(service.background_rpc,'call',healthy)
    with rpc_notices(service.notices):
        await discovery.page(next(iter(service.config.cex)))
    assert attempts==['alchemy','public']
    assert store.rows('funding','SELECT signature FROM jobs')==[{'signature':'funding-gap'}]
    assert not store.is_live('funding-gap',15)
    while not service.notices.queue.empty():
        assert service.notices.queue.get_nowait()[2] is False
