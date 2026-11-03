import asyncio
from dataclasses import replace
import json
import time
from urllib.parse import quote

import pytest
from solders.keypair import Keypair
from features.strategy.token_quality import TokenQuality, social_links, public_url, fetch_metadata, PublicResolver
from trade_execution.executor import Executor
from chain_common.rpc import Priority
from tests.helpers import Notices
from tests.test_pump_buy import setup_order, RPC


@pytest.mark.parametrize('metadata',[
    {'twitter':'https://x.com/project'}, {'extensions':{'telegram':'https://t.me/project'}},
    {'socials':[{'type':'discord','url':'https://discord.gg/project'}]},
    {'links':['https://twitter.com/project']}, {'properties':{'links':{'twitter':'https://x.com/project'}}},
])
def test_declared_social_links(metadata):assert social_links(metadata)


@pytest.mark.parametrize('metadata',[
    {}, {'website':'https://project.example'}, {'image':'https://x.com/project'},
    {'description':'https://x.com/project'}, {'twitter':'https://x.com'},
    {'twitter':'https://x.com/intent/tweet'}, {'twitter':'https://x.com.evil.example/project'},
    {'twitter':'https://x.com@evil.example/project'}, {'twitter':'@project'},
    {'socials':[{},None,'not-a-link']}, {'twitter':'https://discord.com/channels/123'},
    {'twitter':'https://x.com/project?secret=\nvalue'}, {'properties':[]},
])
def test_non_social_or_invalid_links_do_not_admit(metadata):assert not social_links(metadata)


@pytest.mark.parametrize('url',['http://127.0.0.1/a','http://[::1]/a','http://169.254.169.254/a',
                                'file:///secret','https://user:pass@example.com/a','http://10.0.0.1/a'])
def test_private_or_non_http_metadata_rejected(url):
    with pytest.raises(ValueError):public_url(url)


def test_ipfs_and_arweave_metadata_urls():
    assert public_url('ipfs://ipfs/cid/file')=='https://ipfs.io/ipfs/cid/file'
    assert public_url('ar://transaction')=='https://arweave.net/transaction'


@pytest.mark.asyncio
async def test_resolver_checks_actual_dns_addresses():
    resolver=PublicResolver()
    async def resolve(*args):return [{'host':'127.0.0.1'}]
    resolver.delegate.resolve=resolve
    try:
        with pytest.raises(OSError):await resolver.resolve('public-looking.example')
    finally:await resolver.close()


@pytest.mark.asyncio
async def test_inline_metadata_has_no_network_and_uri_is_not_a_social():
    q=TokenQuality()
    uri='data:application/json,'+quote(json.dumps({'twitter':'https://x.com/project'}))
    assert (await q.check(uri,time.time()+1))['allowed']
    assert not (await TokenQuality(fetch=lambda _:None).check('',time.time()+1))['allowed']


@pytest.mark.asyncio
async def test_cache_and_expiry_are_bounded(monkeypatch):
    calls=[];clock=[100.]
    monkeypatch.setattr('features.strategy.token_quality.time.monotonic',lambda:clock[0])
    async def fetch(uri):calls.append(uri);return {'telegram':'https://t.me/project'}
    q=TokenQuality(fetch=fetch)
    first=await q.check('https://metadata.example/1',time.time()+1)
    second=await q.check('https://metadata.example/1',time.time()+1)
    assert first['allowed'] and second['cache_hit'] and len(calls)==1
    clock[0]+=61
    await q.check('https://metadata.example/1',time.time()+1)
    assert len(calls)==2
    for n in range(260):await q.check('https://metadata.example/'+str(n),time.time()+1)
    assert len(q.cache)==256


@pytest.mark.asyncio
async def test_timeout_cancels_http_and_does_not_poison_cache():
    stopped=asyncio.Event()
    async def fetch(uri):
        try:await asyncio.Future()
        finally:stopped.set()
    q=TokenQuality(timeout_ms=20,fetch=fetch)
    result=await q.check('https://metadata.example/a',time.time()+1)
    assert result['reason']=='token-metadata-timeout' and not result['allowed']
    assert stopped.is_set() and not q.cache


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['missing','valid','timeout','unavailable','pending','error'])
async def test_build_completion_uses_only_ready_metadata(config,store,case):
    key=Keypair()
    cfg,r,oid,build=setup_order(replace(config,dry_run=False,wallet_keypair=key,require_social_link=True,signal_age=10),store,key)
    r=replace(r,metadata_uri='https://metadata.example/token')
    started=asyncio.Event();building=asyncio.Event();checked=asyncio.Event()
    cancelled=asyncio.Event();release_cleanup=asyncio.Event();submitted=asyncio.Event()
    async def fetch(uri):
        started.set();await building.wait()
        if case in ('timeout','pending'):
            try:await asyncio.Future()
            finally:
                cancelled.set()
                if case=='pending':await release_cleanup.wait()
        if case=='unavailable':raise OSError('provider-private-error')
        if case=='error':raise RuntimeError('unexpected-private-error')
        return {'twitter':'https://x.com/project'} if case=='valid' else {'image':'https://image.example/logo'}
    checker=TokenQuality(timeout_ms=30 if case=='timeout' else 5000,fetch=fetch)
    original=checker.check
    async def check(*args):
        try:return await original(*args)
        finally:checked.set()
    checker.check=check
    async def builder(*args):
        building.set();await started.wait()
        if case!='pending':await checked.wait()
        return await build(*args)
    class ObservedRPC(RPC):
        async def call(self,method,params):
            result=await super().call(method,params)
            if method=='sendTransaction':submitted.set()
            return result
    rpc=ObservedRPC(store,oid);engine=Executor(cfg,store,rpc,Notices(),Priority(),builder)
    engine.token_quality=checker
    task=asyncio.create_task(engine.buy(oid,r))
    try:
        if case=='pending':
            # Submission must happen even while HTTP cancellation cleanup is blocked.
            await asyncio.wait_for(submitted.wait(),1)
            assert not release_cleanup.is_set()
        else:await asyncio.wait_for(asyncio.shield(task),2)
    finally:
        release_cleanup.set()
        await asyncio.wait_for(task,2)
    row=store.order(oid)
    if case=='missing':
        assert row['reason']=='token-social-link-missing'
        assert row['signature'] is None and not rpc.calls
    else:
        assert row['state']=='submitted' and rpc.calls==['simulateTransaction','sendTransaction']
    decision=store.rows('audit',"SELECT reason,outcome,detail FROM decisions WHERE stage='token-quality'")[0]
    assert json.loads(decision['detail'])['allowed']==(case!='missing')
    assert decision['outcome']==('blocked' if case=='missing' else 'allowed' if case=='valid' else 'bypassed')
    assert decision['reason']=={'missing':'token-social-link-missing','valid':'token-social-link-found',
        'timeout':'token-metadata-timeout','unavailable':'token-metadata-unavailable',
        'pending':'token-metadata-not-ready','error':'token-metadata-unavailable'}[case]
    if case=='pending':assert cancelled.is_set()


@pytest.mark.asyncio
async def test_failed_build_cancels_metadata_task(config,store):
    cfg,r,oid,_=setup_order(replace(config,require_social_link=True),store)
    r=replace(r,metadata_uri='https://metadata.example/a')
    started=asyncio.Event();stopped=asyncio.Event()
    async def fetch(uri):
        started.set()
        try:await asyncio.Future()
        finally:stopped.set()
    async def builder(*args):
        await started.wait();raise ValueError('signal-expired')
    engine=Executor(cfg,store,RPC(),Notices(),Priority(),builder)
    engine.token_quality=TokenQuality(fetch=fetch)
    await engine.buy(oid,r)
    assert stopped.is_set() and store.order(oid)['signature'] is None


def test_quality_enabled_by_default(config):
    from share_common.config import load
    cfg=load(config.data.parent/'config',env={'DRY_RUN':'true'})
    assert cfg.require_social_link and cfg.metadata_timeout_ms==400


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['private-redirect','oversized','valid'])
async def test_http_metadata_is_bounded_and_redirects_cannot_reach_private_network(monkeypatch,kind):
    import features.strategy.token_quality as module
    seen=[]
    class Content:
        async def iter_chunked(self,size):
            if kind=='oversized':
                for _ in range(9):yield b'x'*16384
            else:yield b'{"twitter":"https://x.com/project"}'
    class Response:
        status=302 if kind=='private-redirect' else 200
        headers={'Location':'http://127.0.0.1/secret'}
        content=Content()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def get(self,url,**kwargs):
            seen.append(url);assert kwargs=={'allow_redirects':False};return Response()
    monkeypatch.setattr(module.aiohttp,'TCPConnector',lambda **kwargs:None)
    monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:Session())
    result=await TokenQuality().check('https://metadata.example/file',time.time()+1)
    assert result['allowed']==(kind=='valid')
    assert seen==['https://metadata.example/file']
