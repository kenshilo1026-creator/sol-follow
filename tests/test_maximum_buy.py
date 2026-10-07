import asyncio
import json
import time
from pathlib import Path
from dataclasses import replace
import pytest
from share_common.config import load
from trade_execution.observed_buy import check
from trade_execution.builder import BuildError
from trade_execution.route import decode_all
from chain_common.transaction import Tx
from features.runtime.service import Service
from features.database.storage import Store
from features.audit.dev_audit import report
from tests.test_native_buy import sample
from tests.test_processed import admit,NoRpc


def test_usd_maximum_config_and_range():
    assert load(env={}).max_observed_buy_usd_micros==500_000_000
    assert load(env={'SOL_FOLLOW_MAX_TARGET_BUY_USD':'500.25'}).max_observed_buy_usd_micros==500_250_000
    for value in ['0','-1','NaN','Infinity','0.0000001','9']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MAX_TARGET_BUY_USD':value})


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture',['native','stonk_non_sol_buy.json','pump_non_sol_buy.json'])
@pytest.mark.parametrize('case',['equal','over','missing','cancel'])
async def test_upper_limit_removes_before_other_gates_and_restart(config,store,monkeypatch,fixture,case):
    now=int(time.time())
    raw=sample() if fixture=='native' else json.loads((Path(__file__).parent/'fixtures'/fixture).read_text())
    raw['blockTime']=None;raw['_stream']={'commitment':'processed','live':True}
    route=decode_all(Tx(raw,observed_at=now))[0]
    threshold=5_000_000_000 if fixture=='native' else 1000
    amount=threshold+1 if case in ('over','cancel') else threshold
    if case=='missing' and fixture=='native':amount=0
    route=replace(route,observed_amount=amount)
    admit(store,route,now)
    monkeypatch.setattr('features.runtime.service.decode_buy_routes',lambda tx:[route])
    cfg=replace(config,n=1,hotlist_commitment='processed',min_observed_buy=0,min_observed_buy_usd_micros=0)
    service=Service(cfg,store);service.signals.started=now-1
    async def maximum(r):
        if case=='missing':raise BuildError('price-cache-miss')
        return {'quoteLimit':'1000','maximumUsdMicros':'500000000'}
    service.quote_builder.quote_limit=maximum
    if case=='cancel':
        async def cancel(r):raise asyncio.CancelledError
        service.market_cap.check=cancel
    orders=[]
    class Engine:
        async def buy(self,oid,r):orders.append(oid)
    service.executor=Engine();store.stream_transaction(raw)
    if case=='cancel':
        with pytest.raises(asyncio.CancelledError):await service.process({'signature':route.trade.signature},NoRpc())
    else:await service.process({'signature':route.trade.signature},NoRpc())
    assert bool(orders)==(case=='equal')
    removed=case in ('over','cancel')
    restart=Store(config.data)
    assert (route.trade.wallet not in restart.hotlist(now))==removed
    if removed:
        assert not restart.eligible(route.trade.wallet,route.trade.slot+1,now,now)
        decisions=report(config.data,'wallet',wallet=route.trade.wallet)['decisions']
        assert any(d['stage']=='maximum' and d['detail']['hotlist_removed'] for d in decisions)
    if case!='equal':assert not store.rows('trading','SELECT * FROM votes')


@pytest.mark.asyncio
async def test_usd_cap_payload_and_oracle_prewarm(config,store):
    from tests.test_quote_cache import manager,route
    builder=manager(replace(config,max_observed_buy_usd_micros=500_000_000,
                            min_observed_buy_usd_micros=0,max_market_cap_usd_micros=0),store)
    async def start():pass
    async def exchange(payload):
        assert payload['maximumUsdMicros']=='500000000' and payload['operation']=='quote_limit'
        assert payload['marketCapEnabled']
        return {'quoteLimit':'1000','maximumUsdMicros':'500000000'}
    builder.start=start;builder.exchange=exchange
    assert (await builder.quote_limit(route()))['quoteLimit']=='1000'
