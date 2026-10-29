import asyncio
from dataclasses import replace

import pytest
import aiohttp

from chain_common.availability import Availability
from features.funding.discovery import Discovery
from features.strategy.market_feed import MarketFeed
from tests.helpers import Notices, address


def test_market_sockets_are_independent_and_never_queue_funding(config, store):
    cfg = replace(config, alchemy_key='fixture')
    wallet = address()
    calls = []
    market = MarketFeed(cfg, store, None, Notices(), lambda:{wallet},
                        lambda *args:calls.append(args), lambda _:None)
    assert len(market.feeds) == 2
    assert {f.ws_urls for f in market.feeds} == {(cfg.ws,), (cfg.funding_ws_url,)}
    for f in market.feeds:
        f.refresh()
        assert f.addresses == [wallet] and not f.funding_sources
        f.notification({'params':{'subscription':1,'result':{'context':{'slot':100},
            'value':{'signature':'same-event','err':None}}}}, {wallet:1})
    assert calls == [(wallet,'same-event',100)]*2  # DevSell's durable unique key deduplicates this.
    assert store.rows('funding','SELECT * FROM jobs') == []


def test_market_alert_only_when_combined_coverage_lost_for_over_minute(config, store, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('features.strategy.market_feed.time.monotonic', lambda:now[0])
    notices = Notices()
    market = MarketFeed(replace(config,alchemy_key='fixture'), store, None, notices,
                        lambda:{'wallet'}, lambda *a:None, lambda _:None)
    market.feeds[1].connected=True
    market.feeds[1].subscribed={'wallet'}
    market.observe()
    now[0]=100
    market.observe()
    assert not notices.items  # Primary alone being down is harmless to coverage.
    market.feeds[1].connected=False
    market.feeds[1].subscribed.clear()
    for stamp in (100,160,161,180):
        now[0]=stamp;market.observe()
    assert len(notices.items)==1 and notices.items[0][1]['alert']
    market.feeds[0].connected=True;market.feeds[0].subscribed={'wallet'}
    market.observe()
    assert notices.items[-1][0][0]=='market feed recovered'
    market.feeds[0].connected=False
    market.feeds[0].subscribed.clear()
    now[0]=200;market.observe()
    assert len(notices.items)==2


def test_short_stream_failures_and_recovery_remain_local(monkeypatch):
    now=[0.0]
    monkeypatch.setattr('chain_common.availability.time.monotonic',lambda:now[0])
    notices=Notices();health=Availability(notices,'feed')
    for stamp in (0,60):
        now[0]=stamp;health.failure('down',{})
    assert not any(kw['alert'] for _,kw in notices.items)
    health.recovered('up')
    now[0]=100;health.failure('down',{})
    now[0]=161;health.failure('down',{})
    now[0]=200;health.failure('down',{})
    assert sum(kw['alert'] for _,kw in notices.items)==1


@pytest.mark.asyncio
async def test_funding_websocket_switches_to_public_without_telegram(config, store, monkeypatch):
    cfg=replace(config,alchemy_key='fixture')
    urls=[]
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,url,**kwargs):
            urls.append(url)
            if len(urls)==2:raise asyncio.CancelledError()
            raise aiohttp.WSServerHandshakeError(None,(),status=503)
    async def no_sleep(*args):pass
    monkeypatch.setattr('features.funding.discovery.aiohttp.ClientSession',lambda **kw:Session())
    monkeypatch.setattr('features.funding.discovery.asyncio.sleep',no_sleep)
    notices=Notices()
    with pytest.raises(asyncio.CancelledError):
        await Discovery(cfg,store,None,notices).websocket()
    assert urls==[cfg.funding_ws_url,cfg.ws]
    assert not any(kw.get('alert') for _,kw in notices.items)


@pytest.mark.asyncio
async def test_new_socket_skips_another_feeds_shared_429(config,store,monkeypatch):
    import time
    cfg=replace(config,alchemy_key='fixture')
    first=Discovery(cfg,store,None,Notices())
    first.endpoint_cooldown[cfg.funding_ws_url]=time.monotonic()+90
    urls=[]
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,url,**kwargs):
            urls.append(url);raise asyncio.CancelledError()
    monkeypatch.setattr('features.funding.discovery.aiohttp.ClientSession',lambda **kw:Session())
    second=Discovery(cfg,store,None,Notices())
    with pytest.raises(asyncio.CancelledError):await second.websocket()
    assert urls==[cfg.ws]


@pytest.mark.asyncio
async def test_arm_refreshes_idle_socket_immediately_but_still_requires_acks(config,store,monkeypatch):
    import json
    from types import SimpleNamespace
    import features.funding.discovery as module
    # Freeze this module's clock, not asyncio's. The periodic two-second
    # refresh never becomes due: only the explicit arm event can subscribe.
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:100.0,time=lambda:1000.0))
    messages=asyncio.Queue();sent=asyncio.Queue();receiving=asyncio.Event()
    watches=set();acknowledged=[];read_count=0;cancelled_reads=0
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send_json(self,body):await sent.put(body)
        async def receive(self):
            nonlocal read_count,cancelled_reads
            read_count+=1;receiving.set()
            try:return await messages.get()
            except asyncio.CancelledError:
                cancelled_reads+=1;raise
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def ws_connect(self,*args,**kwargs):return Socket()
    monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:Session())
    market=MarketFeed(replace(config,alchemy_key=''),store,None,Notices(),
        lambda:set(watches),lambda *args:None,acknowledged.append)
    # One provider suffices; no networking or duplicate provider timing in this test.
    market.feeds=market.feeds[:1]
    task=asyncio.create_task(market.feeds[0].websocket());arming=None
    try:
        await asyncio.wait_for(receiving.wait(),1)
        assert sent.empty()
        watches.update({address(),address()})
        arming=asyncio.create_task(market.arm(set(watches)))
        requests=[await asyncio.wait_for(sent.get(),.5) for _ in range(2)]
        assert {r['params'][0]['mentions'][0] for r in requests}==watches
        assert all(r['method']=='logsSubscribe' for r in requests)
        assert not arming.done() and not acknowledged
        assert read_count==1 and cancelled_reads==0
        for request in requests:
            await messages.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':request['id']+100})))
        assert await asyncio.wait_for(arming,.5) is True
        assert set(acknowledged)==watches
        assert cancelled_reads==0  # refresh retained the outstanding receive
    finally:
        for pending in (arming,task):
            if pending is not None and not pending.done():pending.cancel()
        await asyncio.gather(*(t for t in (arming,task) if t is not None),return_exceptions=True)
    assert cancelled_reads==1  # only shutdown cancels the websocket receive
    assert not market.covered()  # disconnected sockets cannot lend old ACKs
    assert market.coverage_changed.is_set()


@pytest.mark.asyncio
async def test_arm_registers_locally_before_scheduling_and_uses_only_required_acks(config,store):
    wallet,token_account,unrelated=address(),address(),address()
    registered=[]
    market=MarketFeed(replace(config,alchemy_key='fixture'),store,None,Notices(),
        lambda:registered.append(True) or {wallet,token_account,unrelated},lambda *a:None,lambda _:None)
    market.feeds[0].subscribed={wallet}
    market.feeds[1].subscribed={token_account}
    # Neither socket has ALL desired addresses; the buy already has coverage.
    assert not any(f.connected for f in market.feeds)
    pending=market.arm({wallet,token_account})
    try:
        assert len(registered)==len(market.feeds)
        assert all(f.changed.is_set() for f in market.feeds)
        assert await asyncio.wait_for(pending,.1) is True
    finally:pending.close()
    assert market.snapshot()['uncovered']==1
    assert await market.arm({unrelated},timeout=.01) is False


@pytest.mark.asyncio
async def test_ack_events_wake_multiple_buyers_without_polling_and_cancel_independently(config,store):
    wallet,first,second=address(),address(),address()
    market=MarketFeed(config,store,None,Notices(),lambda:{wallet,first,second},lambda *a:None,lambda _:None)
    feed=market.feeds[0]
    tasks=[asyncio.create_task(market.arm(required)) for required in ({wallet,first},{wallet,second},{'absent'})]
    try:
        await asyncio.sleep(0)
        tasks[2].cancel()
        await asyncio.gather(tasks[2],return_exceptions=True)
        feed.subscribed.add(wallet);feed.subscription_changed()
        await asyncio.sleep(0)
        assert not tasks[0].done() and not tasks[1].done()
        feed.subscribed.update({first,second});feed.subscription_changed()
        assert await asyncio.wait_for(asyncio.gather(*tasks[:2]),.1)==[True,True]
        feed.subscribed.clear();feed.subscription_changed()
        assert await market.arm({wallet},timeout=.01) is False
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
