"""Offline confirmed funding, atomic family consumption and replay regression tests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
import pytest
from chain_common.primitives import SYSTEM
from chain_common.transaction import Tx
from features.database.storage import Store
from features.funding.decoder import Funding
from features.funding.lineage import Lineage
from features.funding.discovery import Discovery
from features.runtime.service import Service
from features.strategy.dev_create import CreateSignals
from tests.helpers import address, transaction, Notices
from tests.test_aged_funding import launch


def root(store, config, wallet=None):
    lineage=Lineage(store,config)
    item=Funding('root-'+address(),'deposit-'+address(),wallet or address(),next(iter(config.cex)),
                 'cex:test',config.min_funding,10,int(time.time())-30)
    assert store.admit(item,3600,time.time(),lineage=lineage)
    return lineage,item


def payment(config,source,destination=None,*,sig=None,slot=20,amount=None):
    destination=destination or address()
    amount=config.min_funding if amount is None else amount
    ix={'programId':SYSTEM,'parsed':{'type':'transfer','info':{
        'source':source,'destination':destination,'lamports':amount}}}
    raw=transaction([source,destination],[source],[ix],pre=[10**12,1],post=[10**12-amount-5000,1+amount],
                    slot=slot,when=int(time.time())-1,sig=sig or address())
    return Tx(raw)


def child(store,config,lineage,parent,wallet=None):
    tx=payment(config,parent.wallet,wallet)
    items=lineage.observe(tx)
    assert len(items)==1
    assert store.admit(items[0],3600,time.time(),lineage=lineage)
    return items[0]


def test_one_hop_only_and_no_refresh(config,store):
    lineage,parent=root(store,config)
    first=child(store,config,lineage,parent)
    assert not lineage.observe(payment(config,first.wallet,slot=30))
    old=store.hotlist(time.time())[first.wallet]
    again=lineage.observe(payment(config,parent.wallet,first.wallet,slot=21))[0]
    assert not store.admit(again,3600,time.time(),lineage=lineage)
    assert store.hotlist(time.time())[first.wallet]==old


@pytest.mark.parametrize('bad',['unsigned','cex','cpi','program','balance','tiny','too-large','same-slot'])
def test_transfer_proofs(config,store,bad):
    lineage,parent=root(store,config)
    tx=payment(config,parent.wallet)
    if bad=='unsigned':tx.signers.clear()
    if bad=='cex':tx=payment(config,next(iter(config.cex)))
    if bad=='cpi':tx.inner={0:tx.top};tx.top=[]
    if bad=='program':tx.top.append({'programId':address()})
    if bad=='balance':tx.meta['postBalances'][1]=1
    if bad=='tiny':tx=payment(config,parent.wallet,amount=config.min_funding-1)
    if bad=='too-large':tx=payment(config,parent.wallet,amount=config.max_funding+1)
    if bad=='same-slot':tx.slot=parent.slot
    assert lineage.observe(tx)==[]


def test_eleventh_outgoing_removes_parent_dedup_survives_restart(config,store):
    lineage,parent=root(store,config)
    for n in range(10):
        tx=payment(config,parent.wallet,sig='transfer-'+str(n),slot=20+n)
        assert lineage.observe(tx)
        assert lineage.observe(tx)  # Does not count replay twice.
    assert len(store.rows('funding','SELECT * FROM lineage_outgoing'))==10
    lineage=Lineage(Store(config.data),config)
    try:
        assert not lineage.observe(payment(config,parent.wallet,sig='eleventh',slot=40))
        assert parent.wallet not in store.hotlist(time.time())
        later=replace(parent,event='new-deposit',signature='new-deposit',slot=50)
        assert not store.admit(later,3600,time.time(),lineage=lineage)
    finally:lineage.store.close()


def test_multiple_transfers_count_once(config,store):
    lineage,parent=root(store,config)
    tx=payment(config,parent.wallet)
    tx.top.append(tx.top[0].copy())
    lineage.observe(tx)
    assert len(store.rows('funding','SELECT * FROM lineage_outgoing'))==1


@pytest.mark.parametrize('disabled',[False,True])
def test_first_launch_consumes_family_even_rejected_and_restart(config,store,disabled):
    raw,tx,created,now=launch()
    lineage,parent=root(store,config)
    first=child(store,config,lineage,parent,created.user)
    siblings=[child(store,config,lineage,parent) for _ in range(4)]
    if disabled:
        import features.strategy.dev_create as module
        original=module.factory_enabled
        module.factory_enabled=lambda item:False
    try:
        # Age rejection / disabled factory still consumes the entire family.
        signals=CreateSignals(store,replace(config,min_funding_age_seconds=3600),started=0)
        assert signals.prepare(created,tx,True,now)==None
    finally:
        if disabled:module.factory_enabled=original
    assert not store.hotlist(time.time())
    assert store.rows('funding','SELECT * FROM lineage_consumed')[0]['root']==parent.wallet
    reopened=Store(config.data)
    try:
        reloaded=Lineage(reopened,config)
        assert not reloaded.observe(payment(config,parent.wallet,slot=created.slot+1))
        assert not reopened.admit(replace(first,event='renew',signature='renew',slot=created.slot+1),
                                  3600,time.time(),lineage=reloaded)
        assert not reopened.admit(replace(parent,event='root-renew',signature='root-renew',slot=created.slot+1),
                                  3600,time.time(),lineage=reloaded)
    finally:reopened.close()


def test_same_exchange_different_roots_are_independent(config,store):
    _,_,created,now=launch()
    lineage,parent=root(store,config)
    child(store,config,lineage,parent,created.user)
    _,other=root(store,config)
    assert store.consume_dev_create(created,now)[0]
    assert other.wallet in store.hotlist(time.time())


def test_concurrent_sibling_creates_only_one_claim(config,store):
    _,_,one,now=launch();_,_,two,_=launch()
    lineage,parent=root(store,config)
    child(store,config,lineage,parent,one.user);child(store,config,lineage,parent,two.user)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda item:store.consume_dev_create(item,now)[0], [one,two]))
    assert sum(results)==1
    assert not store.hotlist(time.time())


def test_parent_consumed_during_child_qualification_blocks_admit(config,store):
    _,_,created,now=launch()
    lineage,parent=root(store,config,created.user)
    candidate=lineage.observe(payment(config,parent.wallet))[0]
    assert store.consume_dev_create(created,now)[0]
    assert not store.admit(candidate,3600,time.time(),lineage=lineage)


def test_late_parent_requeues_seen_transfer(config,store):
    lineage=Lineage(store,config);wallet=address()
    tx=payment(config,wallet)
    store.enqueue([tx.signature]);store.job_result(tx.signature,'done')
    assert lineage.observe(tx)==[]
    lineage,parent=root(store,config,wallet)
    lineage.admitted(parent)
    assert store.rows('funding','SELECT * FROM jobs')[0]['state']=='pending'
    assert lineage.observe(tx)
    feed=Discovery(config,store,None,Notices(),sources_only=True)
    assert feed.history_address(wallet)
    assert store.rows('funding','SELECT * FROM history_requests')[0]['address']==wallet


def test_invalid_root_funding_revokes_descendant(config,store):
    lineage,parent=root(store,config)
    first=child(store,config,lineage,parent)
    store.invalidate(parent.signature)
    assert not store.hotlist(time.time())
    assert not store.rows('funding','SELECT * FROM funding WHERE wallet=?',(first.wallet,))


def test_only_invalid_chain_create_releases_family(config,store):
    _,_,created,now=launch()
    lineage,parent=root(store,config)
    child(store,config,lineage,parent,created.user)
    assert store.consume_dev_create(created,now)[0]
    store.invalidate(created.signature)
    assert parent.wallet in store.hotlist(time.time())
    assert created.user in store.hotlist(time.time())
    assert not store.rows('funding','SELECT * FROM lineage_consumed')


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed',[False,True])
async def test_child_requires_activity_filter(config,store,monkeypatch,allowed):
    lineage,parent=root(store,config)
    item=lineage.observe(payment(config,parent.wallet))[0]
    calls=[]
    async def activity(candidate,rpc,s,limit):
        calls.append(candidate.event)
        return allowed,'history-qualified' if allowed else 'prelaunch-activity-too-high'
    monkeypatch.setattr('features.runtime.service.qualify_activity',activity)
    class RPC:
        async def call(self,method,params):
            assert method=='getAccountInfo'
            return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
    service=Service(config,store)
    assert await service.qualify(item,RPC()) is allowed
    assert calls==[item.event]
    assert (item.wallet in store.hotlist(time.time())) is allowed


@pytest.mark.asyncio
async def test_confirmed_service_flow_and_failed_payment(config,store,monkeypatch):
    lineage,parent=root(store,config)
    tx=payment(config,parent.wallet)
    service=Service(config,store);calls=[]
    async def qualify(item,rpc):calls.append(item)
    monkeypatch.setattr(service,'qualify',qualify)
    class RPC:
        async def transaction(self,signature):return tx.raw
    store.enqueue([tx.signature])
    await service.process({'signature':tx.signature},RPC())
    assert len(calls)==1 and calls[0].source==parent.wallet
    tx.raw['meta']['err']={'InstructionError':[0,1]};calls.clear()
    await service.process({'signature':tx.signature},RPC())
    assert not calls


def test_default_policy_and_bootstrap_existing_hotlist(config,store):
    assert (config.lineage_max_hops,config.lineage_max_parent_transfers)==(1,10)
    item=Funding('old','old',address(),address(),'cex:test',10**9,10,int(time.time())-10)
    assert store.admit(item,3600,time.time())
    lineage=Lineage(store,config)
    assert lineage.observe(payment(config,item.wallet))


@pytest.mark.asyncio
async def test_processed_waits_for_confirmed_funding_followup(config,store,monkeypatch):
    lineage,parent=root(store,config)
    tx=payment(config,parent.wallet)
    tx.raw['_stream']={'commitment':'processed','live':True}
    store.stream_transaction(tx.raw)
    service=Service(config,store);seen=[]
    async def qualify(item,rpc):seen.append(item)
    monkeypatch.setattr(service,'qualify',qualify)
    class RPC:
        async def transaction(self,signature):return {k:v for k,v in tx.raw.items() if k!='_stream'}
    await service.process({'signature':tx.signature},RPC())
    assert not seen and not store.rows('funding','SELECT * FROM lineage_edges')
    service.proofs.confirmed(tx.signature,'confirmed')
    await service.process({'signature':tx.signature},RPC())
    assert len(seen)==1


def test_expired_parent_and_disabled_discovery_do_not_propagate(config,store):
    lineage,parent=root(store,config)
    with store.db('funding') as c:c.execute('UPDATE hotlist SET expires=0')
    assert lineage.observe(payment(config,parent.wallet))==[]
    assert Lineage(store,replace(config,lineage_enabled=False)).observe(payment(config,parent.wallet))==[]


def test_older_transfer_not_counted_against_new_deposit(config,store):
    lineage,parent=root(store,config)
    assert lineage.observe(payment(config,parent.wallet,slot=parent.slot-1))==[]
    assert not store.rows('funding','SELECT * FROM lineage_outgoing')


def test_lineage_config_bounds():
    from share_common.config import load
    for options in ({'SOL_LINEAGE_MAX_HOPS':'2'},{'SOL_LINEAGE_MAX_HOPS':'-1'},
                    {'SOL_LINEAGE_MAX_PARENT_TRANSFERS':'0'}):
        with pytest.raises(ValueError):load(env=options)
    assert not load(env={'SOL_LINEAGE_ENABLED':'false'}).lineage_enabled


def batch_children(config,lineage,parent,count=20):
    wallets=[address() for _ in range(count)]
    amount=config.min_funding
    instructions=[{'programId':SYSTEM,'parsed':{'type':'transfer','info':{
        'source':parent.wallet,'destination':wallet,'lamports':amount}}} for wallet in wallets]
    tx=Tx(transaction([parent.wallet,*wallets],[parent.wallet],instructions,
        pre=[10**12,*([1]*count)],post=[10**12-amount*count-5000,*([1+amount]*count)],
        slot=20,when=int(time.time())-1,sig=address()))
    items=lineage.observe(tx)
    assert len(items)==count
    return items


@pytest.mark.asyncio
async def test_batch_children_stop_before_rpc_and_retries_share_budget(config,store):
    lineage,parent=root(store,config)
    items=batch_children(config,lineage,parent)
    service=Service(config,store)
    calls=[]
    class RPC:
        async def call(self,method,params):
            assert method=='getAccountInfo'
            calls.append(params[0])
            await asyncio.sleep(0)
            return {'value':None}
    results=await asyncio.gather(*(service.qualify(item,RPC()) for item in items))
    assert not any(results)
    assert len(calls)==len(set(calls))==10
    assert len(store.rows('funding','SELECT * FROM lineage_outgoing'))==1
    assert len(store.rows('funding','SELECT * FROM lineage_child_checks'))==10
    checked=next(item for item in items if item.wallet in calls)
    assert not await service.qualify(replace(checked,event='retry',signature='retry'),RPC())
    assert len(calls)==11
    assert len(store.rows('funding','SELECT * FROM lineage_child_checks'))==10
    rejected=next(item for item in items if item.wallet not in calls)
    assert not await service.qualify(rejected,RPC())
    assert len(calls)==11


def test_child_budget_atomic_across_connections_and_persistent(config,store):
    lineage,parent=root(store,config)
    items=batch_children(config,lineage,parent)
    with ThreadPoolExecutor(8) as pool:
        results=list(pool.map(lineage.reserve_child_check,items))
    assert sum(ok for ok,_ in results)==10
    accepted=[item for item,(ok,_) in zip(items,results) if ok]
    rejected=next(item for item,(ok,_) in zip(items,results) if not ok)
    # Rolling back payment evidence does not undo work already performed.
    store.invalidate(items[0].signature)
    reopened=Store(config.data)
    try:
        reloaded=Lineage(reopened,config)
        assert not reloaded.reserve_child_check(rejected)[0]
        assert reloaded.reserve_child_check(accepted[0])[0]
        assert len(reopened.rows('funding','SELECT * FROM lineage_child_checks'))==10
    finally:
        reopened.close()


def test_existing_children_seed_budget_and_roots_are_independent(config,store):
    lineage,parent=root(store,config)
    items=batch_children(config,lineage,parent)
    for item in items[:10]:
        assert store.admit(item,3600,time.time(),lineage=lineage)
    reloaded=Lineage(store,config)
    assert not reloaded.reserve_child_check(items[10])[0]
    _,other=root(store,config)
    candidate=reloaded.observe(payment(config,other.wallet))[0]
    assert reloaded.reserve_child_check(candidate)[0]


@pytest.mark.asyncio
async def test_cancelled_child_check_keeps_reservation(config,store):
    lineage,parent=root(store,config)
    item=lineage.observe(payment(config,parent.wallet))[0]
    started=asyncio.Event()
    class RPC:
        async def call(self,method,params):
            started.set()
            await asyncio.Event().wait()
    service=Service(config,store)
    task=asyncio.create_task(service.qualify(item,RPC()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    rows=store.rows('funding','SELECT * FROM lineage_child_checks')
    assert len(rows)==1 and rows[0]['wallet']==item.wallet
