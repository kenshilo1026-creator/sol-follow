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
    for stamp in (100,160,161,180):
        now[0]=stamp;market.observe()
    assert len(notices.items)==1 and notices.items[0][1]['alert']
    market.feeds[0].connected=True;market.feeds[0].subscribed={'wallet'}
    market.observe()
    assert notices.items[-1][0][0]=='market feed recovered'
    market.feeds[0].connected=False
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
