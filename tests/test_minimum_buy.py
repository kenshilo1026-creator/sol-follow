import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
import pytest
from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.database.storage import Store
from features.funding.decoder import Funding
from features.funding.grpc_feed import HotlistFeed
from features.runtime.service import Service
from share_common.config import load
from trade_execution.observed_buy import check_minimum
from trade_execution.route import decode_all
from tests.helpers import address
from tests.test_signals import fund,trade
from tests.test_native_buy import sample
from tests.test_processed import admit,NoRpc


def test_minimum_config(config):
    assert load(env={'SOL_FOLLOW_MIN_TARGET_BUY_SOL':'0.1'}).min_observed_buy==100_000_000
    for value in ['-1','NaN','0.0000000001','6']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MIN_TARGET_BUY_SOL':value})
    assert load(env={'SOL_FOLLOW_MIN_TARGET_BUY_SOL':'0'}).min_observed_buy==0


@pytest.mark.asyncio
@pytest.mark.parametrize('paid,allowed',[(99_999_999,False),(100_000_000,True),(100_000_001,True)])
async def test_native_actual_payment_not_large_calldata_budget(config,paid,allowed):
    route=SimpleNamespace(trade=replace(trade(address(),address(),'buy',time.time()),quote=paid),
                          quote_mint=WSOL,observed_amount=5_000_000_000)
    result,detail=await check_minimum(replace(config,min_observed_buy=100_000_000),route,None)
    assert result is allowed
    assert detail['paid_raw']==str(paid)


@pytest.mark.asyncio
@pytest.mark.parametrize('paid,allowed',[(999,False),(1000,True),(1001,True)])
async def test_non_sol_minimum_uses_cached_threshold(config,paid,allowed):
    from tests.test_quote_cache import route as non_sol
    route=non_sol();route=replace(route,trade=replace(route.trade,quote=paid),observed_amount=999999999)
    class Cache:
        async def quote_minimum(self,r):return {'quoteLimit':'1000','solLimit':'333333334','minimumUsdMicros':'50000000'}
    result,detail=await check_minimum(replace(config,min_observed_buy=0,min_observed_buy_usd_micros=50_000_000),route,Cache())
    assert result is allowed and detail['quote_minimum_raw']=='1000'


@pytest.mark.asyncio
async def test_unknown_minimum_is_not_reported_as_below(config):
    from tests.test_quote_cache import route
    from trade_execution.builder import BuildError
    class Cache:
        async def quote_minimum(self,r):raise BuildError('price-cache-miss')
    allowed,detail=await check_minimum(replace(config,min_observed_buy_usd_micros=50_000_000),route(),Cache())
    assert not allowed and detail['reason']=='price-cache-miss'


def test_removal_survives_restart_old_funding_and_invalidation(config,store):
    now=time.time();w,mint=address(),address();fund(store,w,now)
    buy=trade(w,mint,'small',now,slot=5)
    store.remove_hotlist(buy,'buy-below-minimum')
    restarted=Store(config.data)
    assert w not in restarted.hotlist(now)
    assert not restarted.eligible(w,6,now,now)
    assert not HotlistFeed(config,restarted,None,None,None).addresses()
    older=Funding('delayed','delayed',w,address(),'cex:test',10**9,3,int(now)-1)
    assert not restarted.admit(older,3600,now)
    restarted.invalidate('delayed')
    assert w not in restarted.hotlist(now)
    assert not restarted.eligible(w,6,now,now)
    newer=replace(older,event='new',signature='new',slot=6)
    assert restarted.admit(newer,3600,now)
    assert w in restarted.hotlist(now) and restarted.eligible(w,7,now,now)
    # Replaying the old buy cannot consume newer funding.
    restarted.remove_hotlist(buy,'buy-below-minimum')
    assert w in restarted.hotlist(now) and restarted.eligible(w,7,now,now)
    restarted.invalidate('new')
    assert w not in restarted.hotlist(now)


@pytest.mark.asyncio
@pytest.mark.parametrize('paid,orders',[(99_999_999,0),(100_000_000,1)])
async def test_processed_minimum_skips_order_and_removes_subscription(config,store,monkeypatch,paid,orders):
    now=int(time.time());raw=sample();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    route=decode_all(Tx(raw,observed_at=now))[0]
    route=replace(route,trade=replace(route.trade,quote=paid),observed_amount=500_000_000)
    admit(store,route,now)
    monkeypatch.setattr('features.runtime.service.decode_buy_routes',lambda tx:[route])
    cfg=replace(config,n=1,hotlist_commitment='processed',min_observed_buy=100_000_000)
    service=Service(cfg,store);service.signals.started=now-1
    executed=[]
    class Engine:
        async def buy(self,oid,r):executed.append(oid)
    service.executor=Engine();store.stream_transaction(raw)
    await service.process({'signature':route.trade.signature},NoRpc())
    assert len(executed)==orders
    assert len(store.rows('trading','SELECT * FROM orders'))==orders
    if not orders:
        assert not store.rows('trading','SELECT * FROM votes')
        assert route.trade.wallet not in HotlistFeed(cfg,store,None,None,None).addresses()
        assert not Store(config.data).eligible(route.trade.wallet,route.trade.slot+1,now,now)


@pytest.mark.asyncio
async def test_builder_passes_minimum_not_maximum_to_worker(config,store):
    from tests.test_quote_cache import manager,route
    builder=manager(replace(config,min_observed_buy_usd_micros=50_000_000),store)
    async def start():pass
    async def exchange(payload):
        assert payload['operation']=='quote_minimum' and payload['minimumUsdMicros']=='50000000'
        assert payload['marketCapEnabled']  # warm SOL/USD even with market-cap filter disabled
        return {'quoteLimit':'1000','solLimit':'333333334','minimumUsdMicros':'50000000'}
    builder.start=start;builder.exchange=exchange
    assert (await builder.quote_minimum(route()))['quoteLimit']=='1000'


@pytest.mark.asyncio
async def test_minimum_removal_is_durable_before_cancelled_market_cap(config,store,monkeypatch):
    now=int(time.time());raw=sample();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    route=decode_all(Tx(raw,observed_at=now))[0]
    route=replace(route,trade=replace(route.trade,quote=1))
    admit(store,route,now)
    monkeypatch.setattr('features.runtime.service.decode_buy_routes',lambda tx:[route])
    cfg=replace(config,n=1,hotlist_commitment='processed',min_observed_buy=100_000_000)
    service=Service(cfg,store);service.signals.started=now-1
    async def interrupted(r):raise asyncio.CancelledError
    service.market_cap.check=interrupted;store.stream_transaction(raw)
    with pytest.raises(asyncio.CancelledError):
        await service.process({'signature':route.trade.signature},NoRpc())
    assert route.trade.wallet not in Store(config.data).hotlist(now)
    assert not store.rows('trading','SELECT * FROM orders')


def test_usd_minimum_default_and_validation():
    assert load(env={}).min_observed_buy_usd_micros==50_000_000
    assert load(env={'SOL_FOLLOW_MIN_TARGET_BUY_USD':'0'}).min_observed_buy_usd_micros==0
    assert load(env={'SOL_FOLLOW_MIN_TARGET_BUY_USD':'50.25'}).min_observed_buy_usd_micros==50_250_000
    for value in ['-1','NaN','Infinity','0.0000001','1e50']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MIN_TARGET_BUY_USD':value})


@pytest.mark.asyncio
async def test_usd_disable_does_not_fall_back_to_sol_minimum(config):
    from tests.test_quote_cache import route
    r=route();r=replace(r,trade=replace(r.trade,quote=1))
    allowed,detail=await check_minimum(replace(config,min_observed_buy=5_000_000_000,
        min_observed_buy_usd_micros=0),r,None)
    assert allowed and detail['reason']=='minimum-disabled'


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture',['stonk_non_sol_buy.json','pump_non_sol_buy.json'])
@pytest.mark.parametrize('paid,allowed,removed',[(999,False,True),(1000,True,False),(None,False,False)])
async def test_usd_service_removal_and_missing_price(config,store,monkeypatch,fixture,paid,allowed,removed):
    import json
    from pathlib import Path
    from trade_execution.builder import BuildError
    now=int(time.time());raw=json.loads((Path(__file__).parent/'fixtures'/fixture).read_text());raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    route=decode_all(Tx(raw,observed_at=now))[0]
    route=replace(route,trade=replace(route.trade,quote=paid or 1))
    admit(store,route,now)
    monkeypatch.setattr('features.runtime.service.decode_buy_routes',lambda tx:[route])
    cfg=replace(config,n=1,hotlist_commitment='processed',min_observed_buy=0,
                min_observed_buy_usd_micros=50_000_000)
    service=Service(cfg,store);service.signals.started=now-1
    async def minimum(r):
        if paid is None:raise BuildError('market-cap-price-unavailable')
        return {'quoteLimit':'1000','solLimit':'333333334','minimumUsdMicros':'50000000'}
    async def maximum(r):return {'quoteLimit':str(r.observed_amount)}
    service.quote_builder.quote_minimum=minimum;service.quote_builder.quote_limit=maximum
    executed=[]
    class Engine:
        async def buy(self,oid,r):executed.append(oid)
    service.executor=Engine();store.stream_transaction(raw)
    await service.process({'signature':route.trade.signature},NoRpc())
    assert bool(executed)==allowed
    assert (route.trade.wallet not in Store(config.data).hotlist(now))==removed
    if not allowed:assert not store.rows('trading','SELECT * FROM votes')
