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


def sample():
    return json.loads((FIX/'stonk_non_sol_buy.json').read_text())


def test_actual_stonk_buy_uses_net_received_and_ignores_unspent_quote():
    raw=sample()
    route,=decode_all(Tx(raw))
    assert route.kind=='sol_to_stonk_curve'
    assert route.trade.tokens==5624421625112
    assert route.trade.quote==4641142
    assert route.quote_mint=='DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF'
    assert route.request()['pool']=='5AbahsTxvSHhuLJZdQnHEazTav5cgTux3Q9Jcj8Yfh37'


@pytest.mark.parametrize('filename',['stonk_non_sol_buy.json','pump_native_multi_buy.json'])
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


def test_stonk_buy_votes_and_reserves_only_once(config,store):
    from features.funding.decoder import Funding
    from features.strategy.signals import Signals
    route,=decode_stonk(Tx(sample()))
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
