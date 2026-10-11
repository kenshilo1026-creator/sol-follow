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
    assert load(env={'SOL_FOLLOW_MAX_USD':'500.25'}).max_observed_buy_usd_micros==500_250_000
    for value in ['0','-1','NaN','Infinity','0.0000001','9']:
        with pytest.raises(ValueError):load(env={'SOL_FOLLOW_MAX_USD':value})


@pytest.mark.asyncio
@pytest.mark.parametrize('non_sol',[False,True])
@pytest.mark.parametrize('failure',[False,True])
async def test_create_size_uses_own_buy_amount_and_consumption_survives_restart(config,store,non_sol,failure):
    from tests.create_snipe_helpers import setup_create
    cfg=replace(config,n=99,buy_amount=12345,max_observed_buy=1)
    case=setup_create(cfg,store,non_sol=non_sol,fail=failure)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1
    assert store.order(case.calls[0][0])['amount']=='12345'
    restarted=Store(config.data)
    assert case.item.user not in restarted.hotlist(time.time())
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1


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
