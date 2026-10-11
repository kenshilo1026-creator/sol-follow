"""Local entry decisions: public fixtures, boundaries, incomplete data and zero RPC."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import time

import pytest
from chain_common.transaction import Tx
from chain_common.primitives import ata,pubkey
from features.strategy.create_amount import analyze,AmountUnavailable
from features.strategy.create_dev_gate import CreateDevGate,permitted
from features.funding.processed import Processed
from launchpads.create import decode
from tests.helpers import address,Notices
from tests.test_create_decoders import pump
from tests.create_snipe_helpers import token_snapshot,setup_create
from trade_execution.executor import Executor


FIX=Path(__file__).parent/'fixtures/create_amount_live'
SAMPLES=json.loads((FIX/'manifest.json').read_text())


def fresh(amount=0):
    raw=pump();raw['blockTime']=int(time.time());item=decode(Tx(raw))[0]
    token_snapshot(raw,item,amount)
    return raw,decode(Tx(raw))[0]


@pytest.mark.parametrize('sample',SAMPLES,ids=lambda r:r['name'])
def test_nine_current_mainnet_creates_are_decidable_without_account_rpc(sample):
    raw=json.loads((FIX/sample['fixture']).read_text());tx=Tx(raw);item=decode(tx)[0]
    result=analyze(item,tx)
    assert result['post_hold_raw']==sample['dev_post_amount_raw']
    assert result['buy_received_raw']==sample['dev_post_amount_raw']
    if result['calldata_requested_raw'] is not None:
        assert result['calldata_requested_raw']==sample['dev_post_amount_raw']
    assert result['quantity_source'] in ('transaction-meta','calldata+transaction-meta')


@pytest.mark.parametrize('amount,allowed',[(59_999_999_999_999,True),(60_000_000_000_000,True),
                                         (60_000_000_000_001,False),(0,True)])
def test_token_limit_is_decided_synchronously_and_persisted(config,store,amount,allowed):
    raw,item=fresh(amount);tx=Tx(raw)
    from features.strategy.dev_create import create_route
    route=create_route(item,tx)
    cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    gate=CreateDevGate(cfg,store,Notices())
    assert gate.start_create(item,tx,route) is allowed
    assert gate.row(item.mint)['state']==('allowed' if allowed else 'blocked')
    assert permitted(store,route,cfg.max_dev_holding_tokens) is allowed
    assert CreateDevGate(cfg,store,Notices()).start_create(item,tx,route) is allowed


@pytest.mark.parametrize('mutation',['missing','owner','program','decimals','duplicate','negative','huge','index','pre-balance'])
def test_incomplete_or_inconsistent_quantity_is_not_zero(mutation):
    raw,item=fresh(100)
    row=raw['meta']['postTokenBalances'][-1]
    if mutation=='missing':raw['meta'].pop('postTokenBalances')
    elif mutation=='owner':row.pop('owner')
    elif mutation=='program':row['programId']=address()
    elif mutation=='decimals':row['uiTokenAmount']['decimals']+=1
    elif mutation=='duplicate':raw['meta']['postTokenBalances'].append(deepcopy(row))
    elif mutation=='negative':row['uiTokenAmount']['amount']='-1'
    elif mutation=='huge':row['uiTokenAmount']['amount']=str(2**64)
    elif mutation=='index':row['accountIndex']=True
    elif mutation=='pre-balance':raw['meta']['preTokenBalances']=[deepcopy(row)]
    with pytest.raises((AmountUnavailable,ValueError)):analyze(item,Tx(raw))


def test_transfer_out_does_not_hide_dev_launch_receipts():
    raw,item=fresh(10);dev=ata(item.user,item.mint,item.base_token_program)
    receiver=address();other=ata(receiver,item.mint,item.base_token_program)
    keys=raw['transaction']['message']['accountKeys'];keys.append({'pubkey':other,'signer':False,'writable':True})
    raw['meta']['preBalances'].append(0);raw['meta']['postBalances'].append(0)
    raw['meta']['postTokenBalances'].append({'accountIndex':len(keys)-1,'mint':item.mint,'owner':receiver,
        'programId':item.base_token_program,'uiTokenAmount':{'amount':'100','decimals':item.decimals}})
    raw['transaction']['message']['instructions'].append({'programId':item.base_token_program,
        'parsed':{'type':'transferChecked','info':{'source':dev,'destination':other,'mint':item.mint,
            'authority':item.user,'tokenAmount':{'amount':'100','decimals':item.decimals}}}})
    result=analyze(item,Tx(raw))
    assert result['post_hold_raw']=='10' and result['buy_received_raw']=='110' and result['outgoing_raw']=='100'


def test_unidentified_outflow_destination_is_unavailable():
    raw,item=fresh(10);dev=ata(item.user,item.mint,item.base_token_program)
    raw['transaction']['message']['instructions'].append({'programId':item.base_token_program,
        'parsed':{'type':'transferChecked','info':{'source':dev,'destination':address(),'mint':item.mint,
            'authority':item.user,'tokenAmount':{'amount':'100','decimals':item.decimals}}}})
    with pytest.raises(AmountUnavailable,match='owner-unavailable'):analyze(item,Tx(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize('amount,allowed',[(60_000_000_000_000,True),(60_000_000_000_001,False)])
async def test_service_decides_before_reservation_without_holding_rpc(config,store,amount,allowed):
    cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    case=setup_create(cfg,store)
    token_snapshot(case.raw,case.item,amount);store.stream_transaction(case.raw)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==int(allowed)
    assert len(store.rows('trading','SELECT * FROM orders'))==int(allowed)
    row=case.service.dev_holdings.row(case.item.mint)
    assert row['state']==('allowed' if allowed else 'blocked')
    assert row['state']!='checking'
    assert case.item.user not in store.hotlist(time.time())


@pytest.mark.asyncio
async def test_unknown_quantity_skips_without_late_buy(config,store):
    case=setup_create(replace(config,max_dev_holding_tokens=Decimal('60000000')),store)
    await case.service.process(case.row,case.rpc)
    assert case.calls==[]
    assert case.service.dev_holdings.row(case.item.mint)['state']=='unavailable'
    token_snapshot(case.raw,case.item,0);store.stream_transaction(case.raw)
    await case.service.process(case.row,case.rpc)
    assert case.calls==[]  # A later payload cannot resurrect a consumed create.


def test_old_pending_or_rpc_quantity_cannot_authorize_create(config,store):
    raw,item=fresh(10);tx=Tx(raw)
    from features.strategy.dev_create import create_route
    route=create_route(item,tx);cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    with store.db('trading') as db:
        db.execute('INSERT INTO token_dev_holdings VALUES (?,?,?,?,?,?,?)',
            (item.mint,item.signature,item.user,item.slot,time.time(),'checking',json.dumps({'amount_raw':'0','decimals':6})))
    assert not permitted(store,route,cfg.max_dev_holding_tokens)
    assert not store.reserve(cfg,item.mint,item.pool,cfg.buy_amount,allow_pending_dev=False)
    with store.db('trading') as db:db.execute("UPDATE token_dev_holdings SET state='allowed'")
    assert not permitted(store,route,cfg.max_dev_holding_tokens)
    gate=CreateDevGate(cfg,store,Notices())
    assert gate.start_create(item,tx,route)
    assert permitted(store,route,cfg.max_dev_holding_tokens)


def test_saved_over_limit_is_not_reopened_by_raising_limit(config,store):
    raw,item=fresh(60_000_000_000_001);tx=Tx(raw)
    from features.strategy.dev_create import create_route
    route=create_route(item,tx);cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    assert not CreateDevGate(cfg,store,Notices()).start_create(item,tx,route)
    assert not CreateDevGate(replace(cfg,max_dev_holding_tokens=Decimal('99999999')),store,Notices()).start_create(item,tx,route)



def test_internal_dev_transfer_does_not_double_count_received_tokens():
    raw,item=fresh(10);dev=ata(item.user,item.mint,item.base_token_program)
    other=address();keys=raw['transaction']['message']['accountKeys']
    keys.append({'pubkey':other,'signer':False,'writable':True})
    raw['meta']['preBalances'].append(0);raw['meta']['postBalances'].append(0)
    raw['meta']['postTokenBalances'].append({'accountIndex':len(keys)-1,'mint':item.mint,'owner':item.user,
        'programId':item.base_token_program,'uiTokenAmount':{'amount':'100','decimals':item.decimals}})
    raw['transaction']['message']['instructions'].append({'programId':item.base_token_program,
        'parsed':{'type':'transferChecked','info':{'source':dev,'destination':other,'mint':item.mint,
            'authority':item.user,'tokenAmount':{'amount':'100','decimals':item.decimals}}}})
    assert analyze(item,Tx(raw))['buy_received_raw']=='110'


def test_internal_stonk_transfer_fee_is_counted_as_tokens_consumed():
    from tests.test_create_decoders import launchlab
    raw=launchlab();raw['blockTime']=int(time.time());item=decode(Tx(raw))[0]
    token_snapshot(raw,item,10);dev=ata(item.user,item.mint,item.base_token_program)
    other=address();keys=raw['transaction']['message']['accountKeys']
    keys.append({'pubkey':other,'signer':False,'writable':True})
    raw['meta']['preBalances'].append(0);raw['meta']['postBalances'].append(0)
    raw['meta']['postTokenBalances'].append({'accountIndex':len(keys)-1,'mint':item.mint,'owner':item.user,
        'programId':item.base_token_program,'uiTokenAmount':{'amount':'99','decimals':item.decimals}})
    raw['transaction']['message']['instructions'].append({'programId':item.base_token_program,
        'parsed':{'type':'transferChecked','info':{'source':dev,'destination':other,'mint':item.mint,
            'authority':item.user,'tokenAmount':{'amount':'100','decimals':item.decimals}}}})
    result=analyze(item,Tx(raw))
    assert result['post_hold_raw']=='109' and result['buy_received_raw']=='110'


def test_stonk_vesting_is_not_treated_as_zero_liquid_tokens():
    from tests.test_create_decoders import launchlab
    raw=launchlab();item=decode(Tx(raw))[0]
    item=replace(item,flags={**item.flags,'locked_raw':'1'})
    with pytest.raises(AmountUnavailable,match='vesting'):analyze(item,Tx(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize('sample',SAMPLES,ids=lambda r:r['name'])
async def test_mainnet_create_gate_skips_over_limit_before_order_and_without_rpc(config,store,sample):
    from features.funding.decoder import Funding
    from features.runtime.service import Service
    raw=json.loads((FIX/sample['fixture']).read_text());now=int(time.time())
    raw['blockTime']=now;item=decode(Tx(raw))[0]
    store.admit(Funding('live-fixture-fund','fund',item.user,address(),'cex:test',10**9,item.slot-1,now-10),3600,time.time())
    cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'),hotlist_commitment='processed')
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.dev_holdings.notices=service.notices
    calls=[]
    class Engine:
        async def buy(self,oid,route):calls.append(oid)
    class NoRPC:
        async def call(self,*a):raise AssertionError('no extra RPC for create quantity')
        async def transaction(self,*a):raise AssertionError('complete stream must be reused')
    service.executor=Engine();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    store.stream_transaction(raw);store.mark_live(item.signature)
    await service.process({'signature':item.signature},NoRPC())
    allowed=int(sample['dev_post_amount_raw'])<=60_000_000_000_000
    assert len(calls)==int(allowed)
    assert service.dev_holdings.row(item.mint)['state']==('allowed' if allowed else 'blocked')
    assert json.loads(service.dev_holdings.row(item.mint)['detail'])['amount_raw']==sample['dev_post_amount_raw']



def test_explicit_over_limit_pump_calldata_rejects_even_without_balance_metadata(config,store):
    sample=next(r for r in SAMPLES if r['name']=='Loneliness')
    raw=json.loads((FIX/sample['fixture']).read_text())
    from chain_common.primitives import b58decode,b58encode
    for ix in raw['transaction']['message']['instructions']:
        if ix.get('programId')=='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P' and ix.get('data'):
            data=b58decode(ix['data'])
            if data[:8]==bytes([184,23,238,97,103,197,211,61]):
                ix['data']=b58encode(data[:8]+(60_000_000_000_001).to_bytes(8,'little')+data[16:])
    raw['meta'].pop('postTokenBalances')
    tx=Tx(raw);item=decode(tx)[0]
    from features.strategy.dev_create import create_route
    gate=CreateDevGate(replace(config,max_dev_holding_tokens=Decimal('60000000')),store,Notices())
    assert not gate.start_create(item,tx,create_route(item,tx))
    row=gate.row(item.mint)
    assert row['state']=='blocked' and json.loads(row['detail'])['quantity_source']=='calldata'



def test_missing_inner_trace_cannot_make_exact_in_purchase_look_like_zero():
    sample=next(r for r in SAMPLES if r['name']=='Incognito69420')
    raw=json.loads((FIX/sample['fixture']).read_text())
    tx=Tx(raw);item=decode(tx)[0]
    raw['meta']['innerInstructions']=None
    with pytest.raises(AmountUnavailable,match='trace'):analyze(item,Tx(raw))


def test_duplicated_inner_group_is_ambiguous():
    sample=next(r for r in SAMPLES if r['name']=='Incognito69420')
    raw=json.loads((FIX/sample['fixture']).read_text());item=decode(Tx(raw))[0]
    raw['meta']['innerInstructions'].append(deepcopy(raw['meta']['innerInstructions'][0]))
    with pytest.raises(AmountUnavailable,match='trace'):analyze(item,Tx(raw))



def test_recovery_does_not_turn_unknown_old_quantity_into_excess(config,store):
    mint=address()
    with store.db('trading') as db:
        db.execute('INSERT INTO token_dev_holdings VALUES (?,?,?,?,?,?,?)',
            (mint,'old',address(),1,time.time(),'allowed','{}'))
    gate=CreateDevGate(replace(config,max_dev_holding_tokens=Decimal('60000000')),store,Notices())
    gate.recover()
    assert gate.row(mint)['state']=='unavailable'
