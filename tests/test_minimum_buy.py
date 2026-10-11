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
    assert load(env={'SOL_FOLLOW_MIN_SOL':'0.1'}).min_observed_buy==100_000_000
    for value in ['-1','NaN','0.0000000001','6']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MIN_SOL':value})
    assert load(env={'SOL_FOLLOW_MIN_SOL':'0','SOL_FOLLOW_IGNORE_SOL':'0'}).min_observed_buy==0


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
    store.remove_hotlist(buy,'buy-above-maximum')
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
    restarted.remove_hotlist(buy,'buy-above-maximum')
    assert w in restarted.hotlist(now) and restarted.eligible(w,7,now,now)
    restarted.invalidate('new')
    assert w not in restarted.hotlist(now)


@pytest.mark.asyncio
@pytest.mark.parametrize('minimum',[100_000_000,5_000_000_000])
async def test_pure_processed_create_does_not_require_dev_buy_minimum(config,store,minimum):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(replace(config,min_observed_buy=minimum,ignore_observed_buy=minimum,n=99),store)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1 and case.calls[0][1].trade.quote==0
    assert not store.rows('trading','SELECT * FROM votes')
    assert case.item.user not in Store(config.data).hotlist(time.time())


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
async def test_create_consumption_survives_execution_cancellation(config,store):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(config,store)
    async def interrupted(*a):raise asyncio.CancelledError()
    case.service.executor.buy=interrupted
    with pytest.raises(asyncio.CancelledError):await case.service.process(case.row,case.rpc)
    assert case.item.user not in Store(config.data).hotlist(time.time())
    assert store.rows('trading','SELECT state FROM orders')==[{'state':'reserved'}]
    await case.service.process(case.row,case.rpc)
    assert case.calls==[]


def test_usd_minimum_default_and_validation():
    assert load(env={}).min_observed_buy_usd_micros==50_000_000
    assert load(env={'SOL_FOLLOW_MIN_USD':'0','SOL_FOLLOW_IGNORE_USD':'0'}).min_observed_buy_usd_micros==0
    assert load(env={'SOL_FOLLOW_MIN_USD':'50.25'}).min_observed_buy_usd_micros==50_250_000
    for value in ['-1','NaN','Infinity','0.0000001','1e50']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MIN_USD':value})


@pytest.mark.asyncio
async def test_usd_disable_does_not_fall_back_to_sol_minimum(config):
    from tests.test_quote_cache import route
    r=route();r=replace(r,trade=replace(r.trade,quote=1))
    allowed,detail=await check_minimum(replace(config,min_observed_buy=5_000_000_000,
        min_observed_buy_usd_micros=0),r,None)
    assert allowed and detail['reason']=='minimum-disabled'


@pytest.mark.asyncio
@pytest.mark.parametrize('launchpad',['pump','stonk'])
async def test_non_sol_create_needs_no_observed_buyer_usd_valuation(config,store,launchpad):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(replace(config,min_observed_buy_usd_micros=999_000_000,n=99),store,
                      launchpad=launchpad,non_sol=True)
    async def missing(*a):raise AssertionError('observed buyer USD valuation should not run')
    case.service.quote_builder.quote_minimum=missing
    case.service.quote_builder.quote_ignore=missing
    case.service.quote_builder.quote_limit=missing
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1
    assert case.item.user not in Store(config.data).hotlist(time.time())


def test_ignored_small_buy_preserves_prior_vote_and_is_deduped(config,store):
    from features.strategy.signals import Signals
    now=time.time();wallet=address();mint=address();fund(store,wallet,now)
    engine=Signals(store,replace(config,n=2),started=now-1)
    previous=trade(wallet,mint,'large',now)
    engine.observe(previous,now)
    before=store.rows('trading','SELECT * FROM votes')
    small=replace(previous,event='tiny',signature='tiny',slot=previous.slot+1,quote=1)
    store.ignore_vote(small)
    assert engine.observe(small,now) is None
    assert store.rows('trading','SELECT * FROM votes')==before
    assert wallet in Store(config.data).hotlist(now)


@pytest.mark.asyncio
@pytest.mark.parametrize('non_sol',[False,True])
async def test_later_replay_cannot_rearm_consumed_create(config,store,non_sol):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(config,store,non_sol=non_sol,fail=True)
    await case.service.process(case.row,case.rpc)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1
    assert case.item.user not in Store(config.data).hotlist(time.time())
