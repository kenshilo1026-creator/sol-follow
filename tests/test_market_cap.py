import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
import pytest
from features.strategy.market_cap import MarketCapGate,permitted,reset
from features.strategy.signals import Signals
from share_common.config import market_cap_micros,load
from share_common.notify import Notices
from trade_execution.builder import BuildError
from tests.test_signals import fund,trade
from tests.helpers import address


def setup(config,store,cap='20000000000'):
    cfg=replace(config,max_market_cap_usd_micros=20_000_000_000)
    notices=Notices(cfg,store)
    class Builder:
        calls=0
        async def market_cap(self,route):
            self.calls+=1
            return {'marketCapUsdMicros':cap,'snapshotSlot':route.trade.slot}
    builder=Builder();gate=MarketCapGate(cfg,store,notices,builder)
    return cfg,gate,builder,notices


def test_thousands_of_usd_are_exact():
    assert market_cap_micros('20')==20_000_000_000
    assert market_cap_micros('0.5')==500_000_000
    assert market_cap_micros('0')==0
    assert load(env={'SOL_FOLLOW_MAX_MARKET_CAP_USD_K':'20'}).max_market_cap_usd_micros==20_000_000_000


@pytest.mark.parametrize('value',['-1','NaN','Infinity','0.0000000001','1e40'])
def test_invalid_cap(value):
    with pytest.raises(ValueError):market_cap_micros(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('cap,allowed',[('19999999999',True),('20000000000',True),('20000000001',False)])
async def test_first_price_and_boundary_are_frozen(config,store,cap,allowed):
    cfg,gate,builder,notices=setup(config,store,cap)
    now=time.time();mint=address()
    a=SimpleNamespace(trade=trade(address(),mint,'first',now))
    assert await gate.check(a) is allowed
    assert await gate.check(SimpleNamespace(trade=trade(address(),mint,'second',now,slot=3))) is allowed
    restarted=MarketCapGate(cfg,store,notices,builder)
    assert await restarted.check(a) is allowed
    assert builder.calls==1
    assert gate.row(mint)['signature']=='first'
    assert permitted(store,mint,cfg.max_market_cap_usd_micros) is allowed


@pytest.mark.asyncio
async def test_block_stays_after_limit_raised_or_disabled_until_manual_reset(config,store):
    cfg,gate,builder,notices=setup(config,store,'30000000000')
    route=SimpleNamespace(trade=trade(address(),address(),'first',time.time()))
    assert not await gate.check(route)
    for limit in [40_000_000_000,0]:
        other=MarketCapGate(replace(cfg,max_market_cap_usd_micros=limit),store,notices,builder)
        assert not await other.check(route)
    assert reset(store,route.trade.mint)
    assert await other.check(route)
    assert builder.calls==1


@pytest.mark.asyncio
async def test_unknown_first_price_never_uses_later_wallet_price(config,store):
    cfg,gate,builder,notices=setup(config,store)
    async def fail(route):raise BuildError('market-cap-price-unavailable')
    builder.market_cap=fail
    route=SimpleNamespace(trade=trade(address(),address(),'first',time.time()))
    assert not await gate.check(route)
    assert gate.row(route.trade.mint)['state']=='unavailable'
    async def later(route):raise AssertionError('must not replace first snapshot')
    builder.market_cap=later
    assert not await MarketCapGate(cfg,store,notices,builder).check(route)
    assert notices.queue.qsize()==1


@pytest.mark.asyncio
async def test_no_votes_or_orders_before_check_and_under_cap_reaches_n(config,store):
    cfg,gate,builder,notices=setup(config,store)
    now=time.time();mint=address();wallets=[address() for _ in range(3)]
    engine=Signals(store,cfg,started=now-1)
    for w in wallets:fund(store,w,now)
    first=trade(wallets[0],mint,'first',now)
    assert engine.observe(first,now) is None
    assert store.rows('trading','SELECT * FROM votes')==[]
    assert store.reserve(cfg,mint,first.pool,cfg.buy_amount) is None
    assert await gate.check(SimpleNamespace(trade=first))
    for i,w in enumerate(wallets):
        observed=trade(w,mint,str(i),now,slot=i+2)
        assert await gate.check(SimpleNamespace(trade=observed))
        order=engine.observe(observed,now)
        assert bool(order)==(i==2)
    assert builder.calls==1


@pytest.mark.asyncio
async def test_parallel_observations_do_not_replace_first(config,store):
    cfg,gate,builder,notices=setup(config,store)
    first=SimpleNamespace(trade=trade(address(),address(),'first',time.time()))
    results=await asyncio.gather(gate.check(first),gate.check(first))
    assert results==[True,True] and builder.calls==1


@pytest.mark.asyncio
async def test_missing_or_old_snapshot_is_closed(config,store):
    cfg,gate,builder,notices=setup(config,store)
    async def stale(route):return {'marketCapUsdMicros':'1','snapshotSlot':route.trade.slot-1}
    builder.market_cap=stale
    route=SimpleNamespace(trade=trade(address(),address(),'first',time.time()))
    assert not await gate.check(route)
    assert not store.reserve(cfg,route.trade.mint,route.trade.pool,cfg.buy_amount)


@pytest.mark.asyncio
@pytest.mark.parametrize('cap,buys',[('19999999999',1),('20000000001',0)])
async def test_processed_service_checks_before_first_vote(config,store,cap,buys):
    from chain_common.transaction import Tx
    from features.runtime.service import Service
    from tests.test_native_buy import sample
    from tests.test_processed import admit,NoRpc
    from trade_execution.route import decode_all
    now=int(time.time());raw=sample();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    routes=decode_all(Tx(raw,observed_at=now))
    for route in routes:admit(store,route,now)
    cfg=replace(config,n=1,hotlist_commitment='processed',max_market_cap_usd_micros=20_000_000_000)
    service=Service(cfg,store);service.signals.started=now-1
    checked=[];executed=[]
    async def snapshot(route):
        assert not store.rows('trading','SELECT * FROM votes')
        checked.append(route.trade.mint)
        return {'marketCapUsdMicros':cap,'snapshotSlot':route.trade.slot}
    service.quote_builder.market_cap=snapshot
    class Engine:
        async def buy(self,oid,route):executed.append(oid)
    service.executor=Engine()
    store.stream_transaction(raw)
    await service.process({'signature':raw['transaction']['signatures'][0]},NoRpc())
    assert len(checked)==1 and len(executed)==buys
    assert len(store.rows('trading','SELECT * FROM orders'))==buys
    if not buys:assert store.rows('trading','SELECT * FROM votes')==[]
