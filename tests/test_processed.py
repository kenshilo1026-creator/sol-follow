"""Processed execution, source limits and fork evidence (all offline)."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace
import pytest
from chain_common.primitives import b58decode,b58encode,WSOL
from chain_common.transaction import Tx,Unsupported
from chain_common.yellowstone import geyser_pb2 as pb
from features.funding.grpc_feed import HotlistFeed,subscription
from features.funding.processed import Processed
from features.funding.decoder import Funding
from features.runtime.service import Service
from share_common.config import load
from trade_execution.route import decode_all,decode
from trade_execution.native import EXACT
from trade_execution.observed_buy import check
from trade_execution.builder import BuildError
from tests.helpers import address,Notices
from tests.test_native_buy import sample
from tests.test_streaming import wire
FIX=Path(__file__).parent/'fixtures'

class NoRpc:
    async def call(self,*a,**kw):raise AssertionError('no RPC permitted')
    async def transaction(self,*a,**kw):raise AssertionError('no transaction lookup permitted')

class NoQuote:
    async def quote_limit(self,*a):raise AssertionError('native must not quote')


def admit(store,r,now):
    store.admit(Funding('fund'+r.trade.wallet,'fund'+r.trade.wallet,r.trade.wallet,address(),
                'cex:test',10**9,r.trade.slot-1,int(now)-10),3600,now)


def test_config_defaults_and_invalid_limits(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    cfg=load(tmp_path,env={'ALCHEMY_API_KEY':'test-key'})
    assert cfg.hotlist_commitment=='processed' and cfg.max_observed_buy==5_000_000_000
    assert load(tmp_path,env={'SOL_FOLLOW_MAX_TARGET_BUY_SOL':'2.5'}).max_observed_buy==2_500_000_000
    for env in [{'SOL_FOLLOW_MAX_TARGET_BUY_SOL':'0'},{'SOL_FOLLOW_MAX_TARGET_BUY_SOL':'NaN'},
                {'SOL_HOTLIST_COMMITMENT':'processed','SOL_FEED_MODE':'websocket'},
                {'SOL_HOTLIST_COMMITMENT':'pending'}]:
        with pytest.raises(ValueError):load(tmp_path,env=env)


@pytest.mark.asyncio
async def test_native_calldata_upper_bound_not_token_output_or_actual_payment(config):
    raw=sample();ix=raw['transaction']['message']['instructions'][3]
    data=bytearray(b58decode(ix['data']));data[16:24]=(5_000_000_000).to_bytes(8,'little');ix['data']=b58encode(data)
    r=next(r for r in decode_all(Tx(raw)) if r.trade.wallet==ix['accounts'][6])
    assert r.observed_amount==5_000_000_000 and r.observed_kind=='maximum-input-budget'
    assert (await check(config,r,NoQuote()))[0]
    assert not (await check(config,replace(r,observed_amount=5_000_000_001),NoQuote()))[0]
    ix['data']=b58encode(EXACT+(5_000_000_001).to_bytes(8,'little')+(1).to_bytes(8,'little')+b'\x01')
    r=next(r for r in decode_all(Tx(raw)) if r.trade.wallet==ix['accounts'][6])
    assert r.observed_amount==5_000_000_001 and r.observed_kind=='exact-input-budget'
    assert not (await check(config,r,NoQuote()))[0]


@pytest.mark.asyncio
async def test_non_sol_cache_boundary_failure_and_extra_existing_quote(config):
    r=decode(Tx(json.loads((FIX/'pump_non_sol_buy.json').read_text())))
    class Cache:
        calls=0
        async def quote_limit(self,route):
            self.calls+=1
            return {'quoteLimit':str(r.observed_amount),'maximumUsdMicros':'500000000'}
    cache=Cache()
    assert r.observed_mint!=WSOL and r.funding_sol_limit>0
    assert (await check(config,r,cache))[0]
    assert not (await check(config,replace(r,observed_amount=r.observed_amount+1),cache))[0]
    before=cache.calls
    # Non-SOL pairs are governed by USD, independently of the old SOL funding hint.
    assert (await check(config,replace(r,funding_sol_limit=5_000_000_001),cache))[0]
    assert cache.calls==before+1
    class Missing:
        async def quote_limit(self,route):raise BuildError('price-cache-miss')
    assert not (await check(config,r,Missing()))[0]


def test_processed_filter_and_replay_anchor(config,store,monkeypatch):
    req=subscription(['wallet'],commitment='processed')
    assert req.commitment==pb.PROCESSED and req.slots['forks'].interslot_updates
    cfg=replace(config,hotlist_commitment='processed')
    f=HotlistFeed(cfg,store,None,Notices(),None)
    raw=sample();now=raw['blockTime']
    monkeypatch.setattr(time,'time',lambda:now)
    f.live_floor=raw['slot']
    f.anchor=(raw['slot']-1,now,now)
    f.accept(wire(raw));sig=raw['transaction']['signatures'][0]
    assert store.stream_payload(sig)['_stream']['live']
    assert store.stream_payload(sig)['blockTime'] is None
    f.anchor=(raw['slot']-1,now-100,now)
    raw['transaction']['signatures'][0]=b58encode(bytes([7])*64)
    f.accept(wire(raw))
    assert not store.stream_payload(raw['transaction']['signatures'][0])['_stream']['live']
    update=pb.SubscribeUpdate();update.slot.slot=raw['slot'];update.slot.status=pb.SLOT_DEAD
    previous=f.last_slot;f.accept(update)
    assert not Processed(store).usable(sig) and f.last_slot==previous
    assert store.stream_state('processed_cache_generation')==1


@pytest.mark.asyncio
async def test_processed_no_block_time_no_rpc_and_confirmed_dedup(config,store,monkeypatch):
    now=int(time.time());raw=sample();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    sig=raw['transaction']['signatures'][0]
    routes=decode_all(Tx(raw,observed_at=now))
    for r in routes:admit(store,r,now)
    cfg=replace(config,n=1,hotlist_commitment='processed')
    service=Service(cfg,store);service.signals.started=now-1;service.notices=Notices()
    calls=[]
    class Engine:
        async def buy(self,oid,route):calls.append(oid)
    service.executor=Engine()
    def forbidden(*a):raise AssertionError('processed funding must wait for confirmation')
    monkeypatch.setattr('features.runtime.service.funding_decode',forbidden)
    store.stream_transaction(raw)
    await service.process({'signature':sig},NoRpc())
    assert len(calls)==1 and store.rows('trading','SELECT * FROM order_sources')
    assert Processed(store).order_usable(calls[0])
    # A confirmed follow-up must survive completion of the provisional job.
    Processed(store).confirmed(sig,'confirmed')
    store.finish_processed(sig,'processed-provisional',raw['slot'])
    assert store.rows('funding','SELECT reason,state FROM jobs')[0]=={'reason':'confirmed-follow-up','state':'pending'}
    monkeypatch.setattr('features.runtime.service.funding_decode',lambda *a:[])
    confirmed=deepcopy(raw);confirmed.pop('_stream');confirmed['blockTime']=now
    class Rpc:
        async def transaction(self,sig):return confirmed
    await service.process({'signature':sig},Rpc())
    assert len(calls)==1
    Processed(store).invalidate(sig,'different-slot')
    assert not Processed(store).order_usable(calls[0])


@pytest.mark.asyncio
async def test_over_cap_never_votes_even_after_confirmed_retry(config,store):
    now=int(time.time());raw=sample();raw['blockTime']=now
    routes=decode_all(Tx(raw))
    for r in routes:admit(store,r,now)
    service=Service(replace(config,n=1,max_observed_buy=1),store);service.signals.started=now-1
    service.notices=Notices()
    class Rpc:
        async def transaction(self,sig):return raw
    sig=raw['transaction']['signatures'][0]
    store.enqueue([sig]);await service.process({'signature':sig},Rpc())
    assert not store.rows('trading','SELECT * FROM votes')
    assert not store.rows('trading','SELECT * FROM orders')
    assert len(store.rows('trading','SELECT * FROM events'))==4
    service.config=replace(service.config,max_observed_buy=10**12)
    await service.process({'signature':sig},Rpc())
    assert not store.rows('trading','SELECT * FROM orders')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['failure','different-slot','missing','confirmed'])
async def test_reconciler_revokes_or_confirms(config,store,mode):
    raw=sample();raw['_stream']={'commitment':'processed','live':True}
    sig=raw['transaction']['signatures'][0];store.stream_transaction(raw)
    with store.db('funding') as db:db.execute('UPDATE processed_signals SET seen=?',(time.time()-31,))
    state={'err':None,'slot':raw['slot'],'confirmationStatus':'confirmed'}
    if mode=='failure':state['err']={'InstructionError':[0,'bad']}
    elif mode=='different-slot':state['slot']+=1
    elif mode=='missing':state=None
    class Rpc:
        async def call(self,*a):return {'value':[state]}
    signals=SimpleNamespace(invalidate=lambda sig:None)
    proof=Processed(store);await proof.reconcile_once(Rpc(),signals,Notices(),3600)
    assert proof.usable(sig)==(mode=='confirmed')
    assert proof.row(sig)['state']==('confirmed' if mode=='confirmed' else 'uncertain' if mode=='missing' else 'invalid')


def test_receipt_timestamp_requires_explicit_live_proof():
    raw=sample();raw['blockTime']=None
    with pytest.raises(Unsupported):Tx(raw)
    tx=Tx(raw,observed_at=12345)
    assert tx.time==12345 and tx.time_source=='stream-receipt' and raw['blockTime'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate_during_build',[False,True])
async def test_executor_processed_commitment_and_fork_gate(config,store,invalidate_during_build):
    from tests.test_pump_buy import setup_order
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    cfg,route,oid,builder=setup_order(config,store)
    cfg=replace(cfg,hotlist_commitment='processed')
    sig=route.trade.signature
    with store.db('funding') as db:
        db.execute('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?)',
                   (sig,route.trade.slot,time.time(),1,'pending',time.time(),''))
    with store.db('trading') as db:db.execute('INSERT INTO order_sources VALUES (?,?,?)',(oid,sig,route.trade.slot))
    async def build(*args):
        result=await builder(*args)
        if invalidate_during_build:Processed(store).dead_slot(route.trade.slot)
        return result
    class Rpc:
        calls=[]
        async def call(self,method,params):
            self.calls.append(method)
            assert method=='simulateTransaction' and params[1]['commitment']=='processed'
            assert params[1]['minContextSlot']==route.trade.slot
            return {'value':{'err':None}}
    rpc=Rpc()
    await Executor(cfg,store,rpc,Notices(),Priority(),build).buy(oid,route)
    assert store.order(oid)['state']==('failed' if invalidate_during_build else 'dry-simulated')
    assert rpc.calls==([] if invalidate_during_build else ['simulateTransaction'])


@pytest.mark.asyncio
async def test_old_processed_tombstones_retained_for_replay_window_then_pruned(config,store):
    from features.database.maintenance import Maintenance
    now=time.time()
    with store.db('funding') as db:
        db.executemany('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?)',[
            ('old',1,now-90000,1,'invalid',now,'dead-slot'),('recent',2,now-10,1,'invalid',now,'dead-slot')])
        db.executemany('INSERT INTO dead_slots VALUES (?,?)',[(1,now-90000),(2,now-10)])
    await Maintenance(config,store,SimpleNamespace(active=0),Notices()).pass_once()
    assert [r['signature'] for r in store.rows('funding','SELECT * FROM processed_signals')]==['recent']
    assert [r['slot'] for r in store.rows('funding','SELECT * FROM dead_slots')]==[2]


@pytest.mark.asyncio
@pytest.mark.parametrize('proof_state',[None,'confirmed','finalized'])
async def test_processed_mode_never_buys_from_confirmed_http(config,store,proof_state):
    now=int(time.time());raw=sample();raw['blockTime']=now
    sig=raw['transaction']['signatures'][0]
    for route in decode_all(Tx(raw)):admit(store,route,now)
    if proof_state:
        provisional=deepcopy(raw);provisional['_stream']={'commitment':'processed','live':True}
        store.stream_transaction(provisional);Processed(store).confirmed(sig,proof_state)
    else:store.enqueue([sig])
    service=Service(replace(config,n=1,hotlist_commitment='processed'),store)
    service.signals.started=now-1;service.notices=Notices()
    class Rpc:
        async def transaction(self,sig):return raw
    await service.process({'signature':sig},Rpc())
    assert not store.rows('trading','SELECT * FROM votes')
    assert not store.rows('trading','SELECT * FROM orders')
    assert store.rows('funding','SELECT state FROM jobs')[0]['state']=='done'


def test_old_confirmed_votes_cannot_satisfy_processed_threshold(config,store):
    from tests.test_signals import trade,fund
    from features.strategy.signals import Signals
    now=time.time();mint=address();first,second,third=[address() for _ in range(3)]
    for wallet in (first,second,third):fund(store,wallet,now)
    store.vote(trade(first,mint,'legacy',now))
    cfg=replace(config,n=2,hotlist_commitment='processed')
    signals=Signals(store,cfg,started=now-1)
    for sig,wallet in [('new-second',second),('new-third',third)]:
        t=trade(wallet,mint,sig,now)
        with store.db('funding') as db:
            db.execute('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?)',(sig,t.slot,now,1,'pending',now,''))
        oid=signals.observe(t,now)
        assert bool(oid)==(wallet==third)
    sources=store.rows('trading','SELECT signature FROM order_sources')
    assert {r['signature'] for r in sources}=={'new-second','new-third'}


@pytest.mark.asyncio
async def test_trigger_confirmed_during_build_stops_unsent_buy(config,store):
    from tests.test_pump_buy import setup_order
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    cfg,route,oid,builder=setup_order(config,store)
    cfg=replace(cfg,hotlist_commitment='processed')
    sig=route.trade.signature
    with store.db('funding') as db:
        db.execute('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?)',(sig,route.trade.slot,time.time(),1,'pending',time.time(),''))
    with store.db('trading') as db:db.execute('INSERT INTO order_sources VALUES (?,?,?)',(oid,sig,route.trade.slot))
    async def build(*args):
        result=await builder(*args);Processed(store).confirmed(sig,'confirmed');return result
    await Executor(cfg,store,NoRpc(),Notices(),Priority(),build).buy(oid,route)
    assert store.order(oid)['state']=='failed' and not store.order(oid)['signature']
