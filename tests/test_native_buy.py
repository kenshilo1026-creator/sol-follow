from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
import pytest
from chain_common.primitives import b58decode,b58encode
from chain_common.transaction import Tx
from features.funding.decoder import Funding
from features.runtime.service import Service
from tests.helpers import address
from trade_execution.native import EXACT
from trade_execution.route import decode_all


def sample():
    return json.loads((Path(__file__).parent/'fixtures/pump_native_multi_buy.json').read_text())


def test_four_buyers_attributed_to_their_own_instruction():
    routes=decode_all(Tx(sample()))
    assert len(routes)==4 and len({r.trade.wallet for r in routes})==4
    assert [r.trade.quote for r in routes]==[1178186975,1359421567,1400270036,1642059440]
    assert sum(r.trade.tokens for r in routes)==166521591110533
    assert {r.kind for r in routes}=={'pump_native_curve'}
    assert all(r.request()['route']=='pump_native_curve' and not r.dlmm_pool for r in routes)


@pytest.mark.parametrize('mutate',['curve','ata','signer','transfer_source','no_payments','amount','bound','depth','duplicate'])
def test_malformed_buyer_never_borrows_another_buyers_evidence(mutate):
    raw=sample();ix=raw['transaction']['message']['instructions'][3]
    if mutate=='curve':ix['accounts'][3]=address()
    elif mutate=='ata':ix['accounts'][5]=address()
    elif mutate=='signer':
        for k in raw['transaction']['message']['accountKeys']:
            if k['pubkey']==ix['accounts'][6]:k['signer']=False
    elif mutate=='transfer_source':raw['meta']['innerInstructions'][1]['instructions'][1]['parsed']['info']['source']=address()
    elif mutate=='no_payments':
        group=next(g for g in raw['meta']['innerInstructions'] if g['index']==3)
        group['instructions']=[j for j in group['instructions'] if j['programId']!='11111111111111111111111111111111']
    elif mutate in ('amount','bound'):
        data=bytearray(b58decode(ix['data']));at=8 if mutate=='amount' else 16
        data[at:at+8]=(1).to_bytes(8,'little');ix['data']=b58encode(data)
    elif mutate=='depth':
        for g in raw['meta']['innerInstructions']:
            if g['index']==3:
                for j in g['instructions']:j['stackHeight']=3
    elif mutate=='duplicate':
        raw['transaction']['message']['instructions'].append(deepcopy(ix))
        group=deepcopy(next(g for g in raw['meta']['innerInstructions'] if g['index']==3))
        group['index']=10;raw['meta']['innerInstructions'].append(group)
    routes=decode_all(Tx(raw))
    assert len(routes)==3
    assert all(r.trade.wallet!=ix['accounts'][6] for r in routes)


def test_native_exact_input_decoded_with_budget_and_minimum():
    raw=sample();ix=raw['transaction']['message']['instructions'][3]
    ix['data']=b58encode(EXACT+(1178186975).to_bytes(8,'little')+(39000000000000).to_bytes(8,'little')+b'\x01')
    assert len(decode_all(Tx(raw)))==4
    ix['data']=b58encode(EXACT+(100).to_bytes(8,'little')+(39000000000000).to_bytes(8,'little')+b'\x01')
    assert len(decode_all(Tx(raw)))==3


@pytest.mark.asyncio
@pytest.mark.parametrize('eligible,expected',[(0,0),(2,0),(3,1),(4,1)])
async def test_multi_buyer_hotlist_threshold_and_no_retry_within_signature(config,store,eligible,expected):
    raw=sample();raw['blockTime']=int(time.time());routes=decode_all(Tx(raw))
    cfg=replace(config,n=3)
    for i,r in enumerate(routes[:eligible]):
        store.admit(Funding(str(i),'fund',r.trade.wallet,address(),'cex:test',10**9,r.trade.slot-1,raw['blockTime']-10),3600,time.time())
    service=Service(cfg,store);service.signals.started=raw['blockTime']-1
    calls=[]
    class Engine:
        async def buy(self,oid,route):
            calls.append(route)
            store.fail_order(oid,'simulated-test-failure')
    class RPC:
        async def transaction(self,sig):return raw
    service.executor=Engine();row={'signature':routes[0].trade.signature}
    store.enqueue([row['signature']],live=True)
    await service.process(row,RPC());await service.process(row,RPC())
    assert len(calls)==expected
    assert len(store.rows('trading','SELECT * FROM votes'))==eligible
