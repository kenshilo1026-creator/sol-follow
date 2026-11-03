"""Offline processed exit delivery, races and one-simulation submission."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json
import time
import pytest
from solders.keypair import Keypair
from chain_common.transaction import Tx
from features.strategy.dev_sell import DevSell
from features.strategy.create_dev_gate import permitted
from features.funding.processed import Processed
from tests.helpers import Notices,address
from tests.test_dev_sell import armed,sale
from tests.test_dev_exit import setup,receipt
from tests.test_single_buy_simulation import wire


def deliver(worker,raw):
    tx=Tx(raw)
    worker.stream_event(tx.signature,tx.slot,tx.keys)
    worker.stream_receipt(raw,time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize('early',[False,True])
@pytest.mark.parametrize('kind',['pump','pump-v2','stonk'])
async def test_processed_sale_triggers_without_receipt_wait_and_blocks_entry(config,store,early,kind):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    raw,_,_=sale(kind,dev=dev,mint=store.order(oid)['mint'],slot=raw['slot'])
    if early:worker.watches={}
    deliver(worker,raw)
    if early:
        route=SimpleNamespace(trade=SimpleNamespace(wallet=dev,slot=raw['slot']-1,signature='create'),
            request=lambda:json.loads(store.rows('trading','SELECT request FROM order_routes')[0]['request']))
        worker.register(oid,route)
    assert engine.wake.is_set() and reader.calls==0
    assert not permitted(store,SimpleNamespace(trade=SimpleNamespace(mint=store.order(oid)['mint'])),None)
    await worker.once();await engine.once()
    assert reader.calls==0 and builds==[1000]
    deliver(worker,raw);await worker.once();await engine.once()
    assert builds==[1000]
    assert store.rows('trading','SELECT state FROM dev_sell_events')[0]['state']=='triggered'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['failed','missing-meta','other-owner','transfer-only','dead'])
async def test_processed_invalid_sales_never_trigger(config,store,bad):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    if bad=='failed':raw['meta']['err']={'InstructionError':[0,'error']}
    if bad=='missing-meta':raw['meta'].pop('err')
    if bad=='other-owner':
        for name in ('preTokenBalances','postTokenBalances'):raw['meta'][name][0]['owner']=address()
    if bad=='transfer-only':raw['transaction']['message']['instructions']=[]
    if bad=='dead':Processed(store).dead_slot(raw['slot'])
    worker.stream_receipt(raw,time.time())
    assert not store.rows('trading','SELECT * FROM dev_exits') and not builds


@pytest.mark.asyncio
async def test_fork_dies_during_exit_build_cannot_send(config,store):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    deliver(worker,raw)
    builder=engine.builder
    async def race(*args):
        value=await builder(*args)
        Processed(store).dead_slot(raw['slot'])
        return value
    engine.builder=race
    await engine.once()
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='waiting'
    assert dev in worker.watch_addresses()
    assert not any(method=='sendTransaction' for method,_ in engine.rpc.calls)
    later,_,_=sale(dev=dev,mint=store.order(oid)['mint'],slot=raw['slot']+2,sig='replacement-sale')
    engine.builder=builder
    deliver(worker,later);await engine.once()
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='submitted'


@pytest.mark.asyncio
@pytest.mark.parametrize('during_http',[False,True])
async def test_own_processed_fill_unblocks_exit_before_confirmed(config,store,during_http):
    engine,rpc,oid,route,builds=await setup(config,store)
    worker=DevSell(engine.config,store,rpc,Notices(),engine)
    buy=store.order(oid)
    raw=receipt(engine.config.wallet_address,buy['mint'],route.quote_mint,buy['signature'])
    raw['slot']=route.trade.slot+1
    started=asyncio.Event();cancelled=asyncio.Event()
    async def slow(*args,**kwargs):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    rpc.transaction=slow
    if during_http:
        running=asyncio.create_task(engine.once())
        await asyncio.wait_for(started.wait(),1)
        worker.stream_receipt(raw,time.time())
        await asyncio.wait_for(running,1)
        assert cancelled.is_set()
    else:
        worker.stream_receipt(raw,time.time())
        await asyncio.wait_for(engine.once(),1)
        assert not started.is_set()
    assert builds==[1000] and store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='submitted'
    assert store.order(oid)['state']=='submitted'  # Not promoted to finalized accounting.
    assert not store.rows('trading','SELECT * FROM positions')
    assert not engine.fill_waiters


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['unknown-signature','failed','stale','dead','wrong-owner','below-minimum'])
async def test_invalid_own_fill_is_not_executable(config,store,mutation):
    engine,rpc,oid,route,builds=await setup(config,store)
    worker=DevSell(engine.config,store,rpc,Notices(),engine);buy=store.order(oid)
    raw=receipt(engine.config.wallet_address,buy['mint'],route.quote_mint,buy['signature'])
    if mutation=='unknown-signature':raw['transaction']['signatures']=['unrelated']
    if mutation=='failed':raw['meta']['err']='failed'
    if mutation=='wrong-owner':raw['meta']['postTokenBalances'][0]['owner']=address()
    if mutation=='below-minimum':store.update_order(oid,min_out='1001')
    worker.stream_receipt(raw,time.time())
    if mutation=='stale':
        with store.db('trading') as c:c.execute('UPDATE processed_buy_fills SET observed=?',(time.time()-31,))
    if mutation=='dead':Processed(store).dead_slot(raw['slot'])
    assert engine.processed_fill(store.order(oid)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('attestation',['valid','missing','stale'])
async def test_exit_reuses_exact_simulation_and_omits_preflight(config,store,attestation):
    engine,rpc,oid,route,builds=await setup(config,store)
    engine.config=replace(engine.config,hotlist_commitment='processed')
    old=engine.builder
    async def builder(*args):
        result=await old(*args);receiver=Keypair().pubkey()
        _,encoded=wire(engine.config.wallet_address,receiver,1,48000)
        _,sim=wire(engine.config.wallet_address,receiver,1,1400000)
        result.update(transaction=encoded,units=48000,simulation={'version':1,'transaction':sim,
            'commitment':'processed','slot':route.trade.slot,'unitsConsumed':40000,'err':None})
        if attestation=='missing':result.pop('simulation')
        if attestation=='stale':result['simulation']['slot']-=1
        return result
    engine.builder=builder
    await engine.once()
    calls=[(m,p) for m,p in rpc.calls if m!='transaction']
    assert [m for m,p in calls]==(['sendTransaction'] if attestation=='valid' else ['simulateTransaction','sendTransaction'])
    assert calls[-1][1][1]['skipPreflight'] is True
    assert calls[-1][1][1]['preflightCommitment']=='processed'
    assert calls[-1][1][1]['minContextSlot']>=route.trade.slot


@pytest.mark.asyncio
async def test_confirmed_rpc_error_cannot_overwrite_concurrent_processed_trigger(config,store):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    worker.market_event(dev,'dev-sale',raw['slot'])
    async def race(*args,**kwargs):
        worker.stream_receipt(raw,time.time())
        raise TimeoutError()
    reader.transaction=race
    await worker.once()
    assert store.rows('trading','SELECT state FROM dev_sell_events')[0]['state']=='triggered'


@pytest.mark.asyncio
async def test_cached_sell_uses_actual_token_amount_no_buy_deadline(config,store):
    from tests.test_quote_cache import manager,route
    builder=manager(config,store);sent=[]
    async def exchange(payload):sent.append(payload);return {'ok':True}
    builder.exchange=exchange
    request=route().request()
    await builder.sell(config,request,address(),123456)
    assert sent[0]['side']=='sell' and sent[0]['amount']=='123456'
    assert sent[0]['commitment']==config.hotlist_commitment
    assert 'buySignalExpiresAtMs' not in sent[0]


def test_normalized_grpc_receipt_reaches_exit_outside_buy_live_window(config,store):
    from features.funding.grpc_feed import HotlistFeed
    from tests.test_streaming import wire as protobuf
    from tests.test_native_buy import sample
    raw=sample();feed=HotlistFeed(config,store,None,Notices(),None);seen=[]
    feed.position_receipt=lambda receipt,at:seen.append((receipt,at))
    feed.accept(protobuf(raw))
    assert feed.dropped==1 and len(seen)==1
    assert seen[0][0]['transaction']['signatures'][0]==raw['transaction']['signatures'][0]
