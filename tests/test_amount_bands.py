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
@pytest.mark.parametrize('band',['below-ignore','equal-min','above-max','missing-price','cancel'])
async def test_create_does_not_wait_for_observed_buyer_amount_bands(config,store,fixture,band):
    from tests.create_snipe_helpers import setup_create
    cfg=replace(config,n=99,min_observed_buy=5_000_000_000,max_observed_buy=5_000_000_001)
    case=setup_create(cfg,store,launchpad='stonk' if fixture.startswith('stonk') else 'pump',
                      non_sol=fixture!='native')
    async def forbidden(*a):raise AssertionError('create has no observed buyer amount to price')
    case.service.quote_builder.quote_ignore=forbidden
    case.service.quote_builder.quote_minimum=forbidden
    case.service.quote_builder.quote_limit=forbidden
    if band=='cancel':
        async def interrupted(*a):raise asyncio.CancelledError()
        case.service.executor.buy=interrupted
        with pytest.raises(asyncio.CancelledError):await case.service.process(case.row,case.rpc)
    else:await case.service.process(case.row,case.rpc)
    assert len(case.calls)==(0 if band=='cancel' else 1)
    assert case.item.user not in Store(config.data).hotlist(time.time())
    assert not store.rows('trading','SELECT * FROM votes')
