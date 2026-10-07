import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from chain_common.transaction import Tx
from features.database.storage import Store
from features.runtime.service import Service
from share_common.config import load
from trade_execution.builder import BuildError
from trade_execution.route import decode_all
from tests.test_native_buy import sample
from tests.test_processed import admit, NoRpc
from tests.test_quote_cache import manager, route as quote_route


def test_all_six_defaults_and_ordering():
    cfg=load(env={})
    assert (cfg.ignore_observed_buy,cfg.min_observed_buy,cfg.max_observed_buy)==(100_000_000,500_000_000,5_000_000_000)
    assert (cfg.ignore_observed_buy_usd_micros,cfg.min_observed_buy_usd_micros,cfg.max_observed_buy_usd_micros)==(10_000_000,50_000_000,500_000_000)
    for env in ({'SOL_FOLLOW_IGNORE_SOL':'0.6'},{'SOL_FOLLOW_MIN_SOL':'6'},
                {'SOL_FOLLOW_IGNORE_USD':'51'},{'SOL_FOLLOW_MIN_USD':'501'},
                {'SOL_FOLLOW_IGNORE_SOL':'-1'},{'SOL_FOLLOW_IGNORE_USD':'NaN'}):
        with pytest.raises(ValueError):load(env=env)
    cfg=load(env={'SOL_FOLLOW_IGNORE_SOL':'0.2','SOL_FOLLOW_MIN_SOL':'0.6','SOL_FOLLOW_MAX_SOL':'6',
                  'SOL_FOLLOW_IGNORE_USD':'20','SOL_FOLLOW_MIN_USD':'60','SOL_FOLLOW_MAX_USD':'600'})
    assert (cfg.ignore_observed_buy,cfg.min_observed_buy,cfg.max_observed_buy)==(200_000_000,600_000_000,6_000_000_000)
    assert (cfg.ignore_observed_buy_usd_micros,cfg.min_observed_buy_usd_micros,cfg.max_observed_buy_usd_micros)==(20_000_000,60_000_000,600_000_000)


@pytest.mark.asyncio
async def test_ignore_and_minimum_use_distinct_cache_only_payloads(config,store):
    builder=manager(replace(config,ignore_observed_buy_usd_micros=10_000_000,min_observed_buy_usd_micros=50_000_000),store)
    payloads=[]
    async def start():pass
    async def exchange(payload):
        payloads.append(payload)
        return {'minimumUsdMicros':payload['minimumUsdMicros'],'quoteLimit':'1000'}
    builder.start=start;builder.exchange=exchange
    await builder.quote_ignore(quote_route());await builder.quote_minimum(quote_route())
    assert [p['minimumUsdMicros'] for p in payloads]==['10000000','50000000']
    assert all(p['operation']=='quote_minimum' for p in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture',['native','pump_non_sol_buy.json','stonk_non_sol_buy.json'])
@pytest.mark.parametrize('band,removed,follow',[
    ('below-ignore',False,False),('equal-ignore',True,False),('between',True,False),
    ('below-min',True,False),('equal-min',False,True),('equal-max',False,True),
    ('above-max',True,False),('missing-ignore',False,False),('missing-min',False,False),
    ('cancel-min',True,False)])
async def test_processed_three_bands(config,store,monkeypatch,fixture,band,removed,follow):
    now=int(time.time())
    raw=sample() if fixture=='native' else json.loads((Path(__file__).parent/'fixtures'/fixture).read_text())
    raw['blockTime']=None;raw['_stream']={'commitment':'processed','live':True}
    route=decode_all(Tx(raw,observed_at=now))[0]
    native=fixture=='native'
    floor,minimum,maximum=(100_000_000,500_000_000,5_000_000_000) if native else (1000,5000,50000)
    paid={'below-ignore':floor-1,'equal-ignore':floor,'between':floor+1,'below-min':minimum-1,
          'equal-min':minimum,'equal-max':maximum,'above-max':maximum+1,
          'missing-ignore':minimum,'missing-min':minimum,'cancel-min':floor+1}[band]
    if native and band in ('missing-ignore','missing-min'):
        pytest.skip('USD cache failure only applies to non-SOL pairs')
    # A loose maximum input budget must not consume a tiny executed payment.
    route=replace(route,trade=replace(route.trade,quote=paid),observed_amount=maximum+1 if band=='below-ignore' else paid)
    admit(store,route,now)
    monkeypatch.setattr('features.runtime.service.decode_buy_routes',lambda tx:[route])
    cfg=replace(config,n=1,hotlist_commitment='processed',ignore_observed_buy=100_000_000,
                min_observed_buy=500_000_000,ignore_observed_buy_usd_micros=10_000_000,min_observed_buy_usd_micros=50_000_000)
    service=Service(cfg,store);service.signals.started=now-1
    async def ignore(r):
        if band=='missing-ignore':raise BuildError('price-cache-miss')
        return {'quoteLimit':str(floor),'minimumUsdMicros':'10000000'}
    async def minquote(r):
        if band=='missing-min':raise BuildError('price-cache-miss')
        return {'quoteLimit':str(minimum),'minimumUsdMicros':'50000000'}
    async def maxquote(r):return {'quoteLimit':str(maximum),'maximumUsdMicros':'500000000'}
    service.quote_builder.quote_ignore=ignore;service.quote_builder.quote_minimum=minquote;service.quote_builder.quote_limit=maxquote
    if band=='cancel-min':
        async def cancel(r):raise asyncio.CancelledError
        service.market_cap.check=cancel
    orders=[]
    class Engine:
        async def buy(self,oid,r):orders.append(oid)
    service.executor=Engine();store.stream_transaction(raw)
    if band=='cancel-min':
        with pytest.raises(asyncio.CancelledError):service.store.mark_live(route.trade.signature);await service.process({'signature':route.trade.signature},NoRpc())
    else:service.store.mark_live(route.trade.signature);await service.process({'signature':route.trade.signature},NoRpc())
    assert bool(orders)==follow
    restarted=Store(config.data)
    assert (route.trade.wallet not in restarted.hotlist(now))==removed
    if removed:assert not restarted.eligible(route.trade.wallet,route.trade.slot+1,now,now)
    if not follow:
        assert not store.rows('trading','SELECT * FROM votes')
        rows=store.rows('audit',"SELECT * FROM decisions WHERE stage IN ('ignore','minimum','maximum')")
        assert rows and json.loads(rows[-1]['detail'])['hotlist_removed']==removed
        if band=='below-ignore':assert rows[-1]['stage']=='ignore' and rows[-1]['outcome']=='ignored'
