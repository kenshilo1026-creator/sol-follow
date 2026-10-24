"""Offline take-profit lifecycle, unknown sends, rule rearming and exit arbitration."""
import asyncio
import base64
import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest
from solders.transaction import VersionedTransaction

from chain_common.rpc import Priority
from chain_common.primitives import WSOL
from features.strategy.take_profit import TakeProfit, quote_received
from features.strategy.market_index import SCALE
from features.funding.work import WalletFlights
from features.database.storage import Store
from tests.helpers import Notices, transaction, address
from tests.test_dev_exit import setup as exit_setup, blocked


async def setup(config,store,rules=None):
    dev,_,buy_id,route,builds=await exit_setup(config,store)
    cfg=dev.config;mint=route.trade.mint;wallet=cfg.wallet_address
    with store.db('trading') as c:
        c.execute('DELETE FROM token_dev_holdings')
        c.execute('INSERT INTO buy_quote_inputs VALUES (?,?,?)',(buy_id,route.quote_mint,'100'))
    store.fill(buy_id,1000,100)
    rules=rules or [('profit','profit',{'profit_percent':Decimal(300),'sell_percent':Decimal(20)})]
    state={'rate':Decimal('0.5'),'build_rate':None,'send_error':False,'simulation_error':False,'status':None,'height':20,'bad_fill':False}
    class RPC:
        sends=[]
        async def call(self,method,params):
            if method=='simulateTransaction':return {'value':{'err':'bad' if state['simulation_error'] else None}}
            if method=='sendTransaction':
                order=store.rows('trading',"SELECT * FROM orders WHERE side='sell' ORDER BY created DESC")[0]
                assert order['raw']==params[0] and order['signature']
                self.sends.append(params[0])
                if state['send_error']:raise TimeoutError('not logged')
                return order['signature']
            if method=='getSignatureStatuses':return {'value':[state['status']]}
            if method=='getBlockHeight':return state['height']
            raise AssertionError(method)
        async def transaction(self,sig,commitment=None):
            order=store.rows('trading','SELECT * FROM orders WHERE signature=?',(sig,))[0]
            sold=int(order['amount'])-(1 if state['bad_fill'] else 0)
            received=int(order['quoted_out'])
            def balance(index,mint,n):return {'accountIndex':index,'mint':mint,'owner':wallet,'uiTokenAmount':{'amount':str(n)}}
            return transaction([wallet,address(),address()],[wallet],[],sig=sig,when=int(time.time()),
                pre_tokens=[balance(1,mint,10000),balance(2,route.quote_mint,7000)],
                post_tokens=[balance(1,mint,10000-sold),balance(2,route.quote_mint,7000+received)])
    rpc=RPC()
    def quote(amount,rate):
        out=int(amount*rate)
        return dict(side='sell',wallet=wallet,mint=mint,quoteMint=route.quote_mint,amount=str(amount),
                    quotedOut=str(out),minOut=str(max(1,out*98//100)),spotIndex=str(int(rate*SCALE)),indexScale=str(SCALE))
    async def quoter(config,request,wallet,amount):return quote(amount,state['rate'])
    async def builder(config,request,wallet,amount):
        result=await dev.builder(config,request,wallet,amount)
        return {**result,**quote(amount,state['build_rate'] if state['build_rate'] is not None else state['rate'])}
    flights=WalletFlights();dev.flights=flights
    engine=TakeProfit(cfg,store,rpc,Notices(),Priority(),flights=flights,builder=builder,quoter=quoter,rules=rules)
    return engine,state,builds,dev,buy_id


def sells(store):return store.rows('trading',"SELECT * FROM orders WHERE side='sell' ORDER BY created")
def position(store):return store.rows('trading','SELECT * FROM positions')[0]


@pytest.mark.asyncio
async def test_partial_fill_is_atomic_once_and_second_rule_uses_remaining(config,store):
    rules=[('first','profit',{'profit_percent':Decimal(300),'sell_percent':Decimal(20)}),
           ('second','profit',{'profit_percent':Decimal(500),'sell_percent':Decimal(20)})]
    engine,state,builds,_,_=await setup(config,store,rules)
    await engine.once()
    assert builds==[200] and sells(store)[0]['state']=='submitted'
    assert position(store)['rules']=='{}' and position(store)['amount']=='1000'
    state['status']={'confirmationStatus':'finalized','err':None}
    await engine.once();await engine.reconcile(sells(store)[0])
    assert position(store)['amount']=='800'
    assert json.loads(position(store)['rules'])=={'first':'done'}
    sweeps=store.rows('trading','SELECT * FROM quote_sweeps')
    assert len(sweeps)==1 and sweeps[0]['amount']=='100'
    state['rate']=Decimal('0.7');state['status']=None;engine.next.clear()
    await engine.once()
    assert builds==[200,160]


@pytest.mark.asyncio
async def test_unknown_send_restart_never_signs_another_sale(config,store):
    engine,state,builds,_,_=await setup(config,store)
    state['send_error']=True
    await engine.once();original=sells(store)[0]
    assert original['state']=='unknown'
    restarted=TakeProfit(engine.config,Store(config.data),engine.rpc,Notices(),engine.priority,
                         builder=engine.builder,quoter=engine.quoter,rules=engine.rules)
    state['height']=100000
    await restarted.once();await restarted.once()
    assert builds==[200] and len(sells(store))==1 and sells(store)[0]['signature']==original['signature']
    assert position(store)['amount']=='1000' and position(store)['rules']=='{}'


@pytest.mark.asyncio
async def test_finalized_failure_retries_rule_without_marking_done(config,store):
    engine,state,builds,_,_=await setup(config,store)
    await engine.once()
    state['status']={'confirmationStatus':'finalized','err':{'InstructionError':[0,'bad']}}
    await engine.once()
    assert sells(store)[0]['state']=='failed' and position(store)['rules']=='{}'
    state['status']=None;engine.next.clear();await engine.once()
    assert builds==[200,200] and len(sells(store))==2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['simulation','threshold','dev-block'])
async def test_recheck_before_signing_never_sends_invalid_exit(config,store,failure):
    engine,state,builds,_,_=await setup(config,store)
    if failure=='simulation':state['simulation_error']=True
    if failure=='threshold':state['build_rate']=Decimal('0.1')
    if failure=='dev-block':
        build=engine.builder
        async def builder(*args):
            result=await build(*args);blocked(store,position(store)['mint']);return result
        engine.builder=builder
    await engine.once()
    assert not engine.rpc.sends and sells(store)[0]['state']=='failed' and position(store)['rules']=='{}'


@pytest.mark.asyncio
async def test_bad_fill_does_not_complete_rule_or_release_reservation(config,store):
    engine,state,_,_,_=await setup(config,store)
    await engine.once();state['bad_fill']=True;state['status']={'confirmationStatus':'finalized','err':None}
    await engine.once()
    assert sells(store)[0]['state']=='submitted' and position(store)['amount']=='1000'
    assert position(store)['rules']=='{}' and not store.rows('trading','SELECT * FROM quote_sweeps')


@pytest.mark.asyncio
async def test_stop_loss_and_protect_use_supplied_rh_semantics(config,store):
    rules=[('stop','stop_loss',{'loss_percent':Decimal(6),'sell_percent':Decimal(100)}),
           ('protect','protect',{'activate_profit_percent':Decimal(50),'profit_percent':Decimal(20),'sell_percent':Decimal(100)})]
    engine,state,builds,_,_=await setup(config,store,rules)
    state['rate']=Decimal('.16');await engine.once();assert not builds
    # Peak survives a new engine instance.
    engine=TakeProfit(engine.config,store,engine.rpc,Notices(),Priority(),builder=engine.builder,quoter=engine.quoter,rules=rules)
    state['rate']=Decimal('.119');await engine.once()
    assert sells(store)[0]['rule_id']=='protect' and builds==[1000]


@pytest.mark.asyncio
async def test_stop_loss_threshold(config,store):
    rule=[('stop','stop_loss',{'loss_percent':Decimal(6),'sell_percent':Decimal(100)})]
    engine,state,builds,_,_=await setup(config,store,rule)
    state['rate']=Decimal('.093');await engine.once()
    assert builds==[1000] and sells(store)[0]['rule_id']=='stop'


@pytest.mark.asyncio
async def test_trailing_rearms_only_after_new_high(config,store):
    rules=[('trail','trailing',{'activate_profit_percent':Decimal(100),'drawdown_percent':Decimal(20),'sell_percent':Decimal(20)})]
    engine,state,builds,_,_=await setup(config,store,rules)
    state['rate']=Decimal('.3');await engine.once();assert not builds
    engine.next.clear();state['rate']=Decimal('.23');await engine.once();assert builds==[200]
    state['status']={'confirmationStatus':'finalized','err':None};await engine.once()
    state['status']=None;state['rate']=Decimal('.25');engine.next.clear();await engine.once();assert builds==[200]
    state['rate']=Decimal('.4');engine.next.clear();await engine.once()
    assert json.loads(position(store)['rules'])=={}
    state['rate']=Decimal('.3');engine.next.clear();await engine.once();assert builds==[200,160]


@pytest.mark.asyncio
async def test_emergency_waits_for_pending_tp_and_only_sells_remaining(config,store):
    engine,state,_,dev,buy_id=await setup(config,store)
    await engine.once();blocked(store,position(store)['mint'])
    await dev.once();assert not store.rows('trading',"SELECT 1 FROM dev_exits WHERE signature IS NOT NULL")
    state['status']={'confirmationStatus':'finalized','err':None};await engine.once()
    amounts=[]
    async def builder(config,request,wallet,amount):
        amounts.append(amount);raise ValueError('test-stop-before-send')
    dev.builder=builder
    await dev.once();assert amounts==[800]


@pytest.mark.asyncio
async def test_dry_run_never_reads_or_sends_live_positions(config,store):
    engine,state,builds,_,_=await setup(config,store)
    engine.config=replace(engine.config,dry_run=True)
    await engine.once();assert not builds and not sells(store)


@pytest.mark.asyncio
async def test_missing_non_sol_basis_cannot_use_sol_cost(config,store):
    engine,state,builds,_,_=await setup(config,store)
    with store.db('trading') as c:
        c.execute('DELETE FROM buy_quote_inputs')
        c.execute('DELETE FROM position_market_indexes')
    await engine.once();assert not builds and not sells(store)


def test_native_proceeds_neutralize_rent_and_include_retained_wsol():
    wallet,base,out=address(),address(),address()
    def balance(i,mint,n):return {'accountIndex':i,'mint':mint,'owner':wallet,'uiTokenAmount':{'amount':str(n)}}
    raw=transaction([wallet,base,out],[wallet],[],pre=[100000,2000,0],post=[94000,0,4000],
                    pre_tokens=[balance(1,address(),100)],post_tokens=[balance(2,WSOL,2000)])
    # wallet -6000 + fee 5000 + closed ATA -2000 + new WSOL account 4000 = 1000.
    assert quote_received(__import__('chain_common.transaction',fromlist=['Tx']).Tx(raw),wallet,WSOL)==1000


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["buy", "dev"])
async def test_background_quote_yields_and_releases_mint(config,store,interrupt):
    engine,state,builds,_,_=await setup(config,store)
    started=asyncio.Event();cancelled=asyncio.Event()
    async def quoter(*args):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    engine.quoter=quoter
    task=asyncio.create_task(engine.once())
    try:
        await asyncio.wait_for(started.wait(),1)
        if interrupt=='buy':engine.priority.active=1
        else:blocked(store,position(store)['mint'])
        await asyncio.wait_for(task,1)
        assert cancelled.is_set() and not builds and not sells(store)
        assert not engine.flights.entries
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_rules_still_reconciles_pending_sale(config,store):
    engine,state,builds,_,_=await setup(config,store)
    await engine.once()
    engine.rules=[]
    state['status']={'confirmationStatus':'finalized','err':None}
    await engine.once()
    assert position(store)['amount']=='800' and sells(store)[0]['state']=='finalized'
    assert builds==[200]


@pytest.mark.asyncio
async def test_crash_after_reserve_before_route_is_recoverable(config,store):
    from features.strategy.take_profit import MARKER
    engine,state,builds,_,_=await setup(config,store)
    pos=position(store)
    oid=store.reserve(engine.config,pos['mint'],pos['pool'],200,'sell','profit',reason=MARKER)
    engine.rules=[]
    await engine.once()
    assert store.order(oid)['state']=='failed' and not builds


def test_rh_fixed_rules_and_configuration(tmp_path):
    from features.strategy.positions import load_rules
    from share_common.config import load
    path=tmp_path/'rules.json'
    path.write_text(json.dumps({'version':1,'gain_definition':'profit_over_entry_percent','sell_basis':'current_position',
        'rules':[{'type':'fixed','profit_percent':300,'sell_percent':7}]}),encoding='utf-8')
    rule=load_rules(path)[0]
    assert rule[1]=='profit' and rule[2]['sell_percent']==7
    assert load_rules(tmp_path/'missing.json')==[]
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}',encoding='utf-8')
    cfg=load(root=tmp_path,env={'SOL_TAKE_PROFIT_ENABLED':'false','SOL_TAKE_PROFIT_POLL_SECONDS':'8',
                              'SOL_TAKE_PROFIT_RULES_PATH':'rules.json'})
    assert not hasattr(cfg,'take_profit_enabled') and cfg.take_profit_poll_seconds==8 and cfg.take_profit_rules_path=='rules.json'
    for interval in ['0','301','NaN']:
        with pytest.raises(ValueError):load(root=tmp_path,env={'SOL_TAKE_PROFIT_POLL_SECONDS':interval})


def test_take_profit_notice_includes_rule_and_execution_result():
    from share_common.notify import message
    text=message('take profit submitted',{'mint':'mint','rule_type':'profit','thresholds':{'profit_percent':'300'},
        'gain_percent':'310','amount':'200','signature':'signature'})
    assert 'profit' in text and '300' in text and '310' in text and 'signature' in text
    assert '100' in message('take profit finalized',{'mint':'mint','received':'100'})


@pytest.mark.asyncio
async def test_fill_index_persisted_and_independent_of_later_spot_or_remaining(config,store):
    engine,state,builds,_,buy_id=await setup(config,store)
    original=store.rows('trading','SELECT * FROM position_market_indexes')[0]
    assert int(original['entry_index'])==SCALE//10
    state['rate']=Decimal('.2');await engine.once()
    with store.db('trading') as c:c.execute("UPDATE positions SET amount='500'")
    engine.next.clear();await engine.once()
    row=store.rows('trading','SELECT * FROM position_market_indexes')[0]
    assert row['entry_index']==original['entry_index'] and row['registered']==original['registered']
    assert int(row['last_index'])==SCALE//5 and int(row['peak_index'])==SCALE//5
    assert position(store)['peak']=='100' and not builds


@pytest.mark.asyncio
async def test_liquidation_quote_cannot_trigger_stop_or_suppress_spot_profit(config,store):
    rules=[('stop','stop_loss',{'loss_percent':Decimal(6),'sell_percent':Decimal(100)}),
           ('profit','profit',{'profit_percent':Decimal(300),'sell_percent':Decimal(20)})]
    engine,state,builds,_,_=await setup(config,store,rules)
    quote=engine.quoter;build=engine.builder
    async def low_quote(*args):
        result=await quote(*args)
        return {**result,'quotedOut':'1','minOut':'1'}
    async def low_build(*args):
        result=await build(*args)
        return {**result,'quotedOut':'1','minOut':'1'}
    engine.quoter=low_quote;engine.builder=low_build
    state['rate']=Decimal('.1');await engine.once();assert not builds
    state['rate']=Decimal('.5');engine.next.clear();await engine.once()
    assert sells(store)[0]['rule_id']=='profit' and sells(store)[0]['state']=='submitted'


@pytest.mark.asyncio
async def test_legacy_backfill_never_uses_new_spot_or_old_liquidation_peak(config,store):
    engine,state,builds,_,_=await setup(config,store)
    with store.db('trading') as c:
        c.execute('DELETE FROM position_market_indexes')
        c.execute("UPDATE positions SET peak='9000'")
    state['rate']=Decimal('.2');await engine.once()
    row=store.rows('trading','SELECT * FROM position_market_indexes')[0]
    assert int(row['entry_index'])==SCALE//10 and int(row['peak_index'])==SCALE//5
    assert position(store)['peak']=='100' and not builds


@pytest.mark.asyncio
async def test_poll_slowdown_boundary_and_restart(config,store):
    engine,state,builds,_,_=await setup(config,store)
    with store.db('trading') as c:c.execute('UPDATE position_market_indexes SET registered=1000')
    assert engine.poll_interval(position(store),2799.99)==5
    assert engine.poll_interval(position(store),2800)==25
    restarted=TakeProfit(engine.config,Store(config.data),engine.rpc,Notices(),Priority(),rules=engine.rules)
    assert restarted.poll_interval(position(store),3000)==25
    restarted.config=replace(restarted.config,take_profit_poll_seconds=8)
    assert restarted.poll_interval(position(store),3000)==40


@pytest.mark.asyncio
async def test_old_position_is_not_polled_on_every_five_second_tick(config,store,monkeypatch):
    engine,state,builds,_,_=await setup(config,store)
    state['rate']=Decimal('.1')
    with store.db('trading') as c:c.execute('UPDATE position_market_indexes SET registered=?',(time.time()-1900,))
    ticks=[0];calls=[];quote=engine.quoter
    monkeypatch.setattr('features.strategy.take_profit.time.monotonic',lambda:ticks[0])
    async def counted(*args):calls.append(ticks[0]);return await quote(*args)
    engine.quoter=counted
    await engine.once();ticks[0]=5;await engine.once();ticks[0]=24;await engine.once();ticks[0]=25;await engine.once()
    assert calls==[0,25]


@pytest.mark.asyncio
async def test_peak_and_rule_rearm_rollback_together(config,store):
    engine,state,builds,_,_=await setup(config,store)
    with store.db('trading') as c:
        c.execute("CREATE TRIGGER fail_peak BEFORE UPDATE ON positions BEGIN SELECT RAISE(ABORT,'test storage failure'); END")
    await engine.once()
    row=store.rows('trading','SELECT * FROM position_market_indexes')[0]
    assert row['last_index'] is None and int(row['peak_index'])==SCALE//10
    assert position(store)['peak']=='0' and not builds


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('spotIndex','0'),('spotIndex','-1'),('indexScale','1'),('quoteMint','wrong')])
async def test_invalid_spot_never_falls_back_to_liquidation(config,store,field,value):
    engine,state,builds,_,_=await setup(config,store)
    quote=engine.quoter
    async def invalid(*args):return {**await quote(*args),field:value}
    engine.quoter=invalid
    await engine.once()
    assert not builds and not sells(store)
    assert store.rows('trading','SELECT last_index FROM position_market_indexes')[0]['last_index'] is None
