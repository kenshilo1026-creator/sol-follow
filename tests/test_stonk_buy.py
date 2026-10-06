import copy
import json
from pathlib import Path
from dataclasses import replace
import pytest
from chain_common.transaction import Tx
from chain_common.yellowstone.normalize import normalize
from trade_execution.route import decode_all
from trade_execution.stonk import decode_stonk
from tests.test_streaming import wire

FIX=Path(__file__).parent/'fixtures'


def sample(filename='stonk_non_sol_buy.json'):
    return json.loads((FIX/filename).read_text())


def test_native_stonk_exact_budget_and_net_receipt_exclude_rent_tip_and_tax():
    from chain_common.primitives import WSOL
    route,=decode_all(Tx(sample('stonk_native_buy.json')))
    assert route.kind=='stonk_native_curve'
    assert route.quote_mint==route.observed_mint==WSOL
    assert route.observed_amount==route.trade.quote==4340000000
    assert route.trade.tokens==102384232409726
    assert route.trade.mint=='J5hGf8AEr8e1yDUMoRKSvKtN5KG64WNH4G2oErAY6VVZ'
    assert route.request()['pool']=='4PdBiKmehHJ8DAqxcW1DNxNL5CxLRKro8TrLVhHM9vmW'


def test_native_stonk_observed_budget_gate_needs_no_quote_service(config):
    import asyncio
    from trade_execution.observed_buy import check
    route,=decode_stonk(Tx(sample('stonk_native_buy.json')))
    async def scenario():
        # No quote builder exists here: native input is already denominated in SOL.
        allowed,detail=await check(replace(config,max_observed_buy=5000000000),route,None)
        assert allowed and detail['sol_lamports']=='4340000000'
        allowed,detail=await check(replace(config,max_observed_buy=4000000000),route,None)
        assert not allowed and detail['reason']=='source-sol-budget-over-limit'
    asyncio.run(scenario())


@pytest.mark.parametrize('fault',['quote-program','platform','quote-vault','payment','duplicate'])
def test_native_stonk_requires_proven_unique_buy(fault):
    from chain_common.primitives import TOKEN_2022
    raw=sample('stonk_native_buy.json')
    ix=raw['transaction']['message']['instructions'][6]
    if fault=='quote-program': ix['accounts'][12]=TOKEN_2022
    if fault=='platform': ix['accounts'][3]='11111111111111111111111111111111'
    if fault=='quote-vault': ix['accounts'][8]='11111111111111111111111111111111'
    if fault=='payment': raw['meta']['innerInstructions'][-1]['instructions'].pop(1)
    if fault=='duplicate': raw['transaction']['message']['instructions'].append(copy.deepcopy(ix))
    assert decode_stonk(Tx(raw))==[]


def test_actual_stonk_buy_uses_net_received_and_ignores_unspent_quote():
    raw=sample()
    route,=decode_all(Tx(raw))
    assert route.kind=='sol_to_stonk_curve'
    assert route.trade.tokens==5624421625112
    assert route.trade.quote==4641142
    assert route.quote_mint=='DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF'
    assert route.request()['pool']=='5AbahsTxvSHhuLJZdQnHEazTav5cgTux3Q9Jcj8Yfh37'


@pytest.mark.parametrize('filename',['stonk_non_sol_buy.json','stonk_native_buy.json','pump_native_multi_buy.json'])
def test_real_buy_survives_protobuf_and_token_cpi_normalization(filename):
    raw=json.loads((FIX/filename).read_text())
    expected=decode_all(Tx(raw))
    actual=decode_all(Tx(normalize(wire(raw).transaction,raw['blockTime'])))
    assert actual==expected
    assert len(actual)==(1 if filename.startswith('stonk') else 4)


@pytest.mark.parametrize('which',[0,1,3,4,5,6,7,8,13,14])
def test_rejects_unproven_platform_wallet_or_vault(which):
    raw=sample()
    raw['transaction']['message']['instructions'][4]['accounts'][which]='11111111111111111111111111111111'
    assert decode_stonk(Tx(raw))==[]


def test_no_cpi_proof_no_buy_and_duplicate_wallet_mint_is_ambiguous():
    raw=sample()
    raw['meta']['innerInstructions'][-1]['instructions']=[]
    assert decode_stonk(Tx(raw))==[]
    raw=sample()
    raw['transaction']['message']['instructions'].append(copy.deepcopy(raw['transaction']['message']['instructions'][4]))
    duplicate=copy.deepcopy(raw['meta']['innerInstructions'][-1]);duplicate['index']=5
    raw['meta']['innerInstructions'].append(duplicate)
    assert decode_stonk(Tx(raw))==[]


@pytest.mark.parametrize('filename',['stonk_non_sol_buy.json','stonk_native_buy.json'])
def test_stonk_buy_votes_and_reserves_only_once(config,store,filename):
    from features.funding.decoder import Funding
    from features.strategy.signals import Signals
    route,=decode_stonk(Tx(sample(filename)))
    # Exercise the same signal path as Pump; the observed DJT amount must not
    # replace the independently configured SOL spending budget.
    now=route.trade.time
    cfg=replace(config,n=1)
    f=Funding('test-funding','test-funding',route.trade.wallet,'source','cex:test',1000000000,route.trade.slot-1,now-1)
    store.admit(f,cfg.hotlist_ttl,now)
    signals=Signals(store,cfg,started=now-2)
    oid=signals.observe(route.trade,now=now)
    assert oid
    assert int(store.order(oid)['amount'])==cfg.buy_amount
    assert signals.observe(route.trade,now=now) is None
