"""Create sniping uses decoded launch evidence; all RPC/builders are offline."""
import asyncio
import base64
from dataclasses import replace
import time

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from chain_common.primitives import WSOL, pubkey
from chain_common.transaction import Tx
from features.funding.decoder import Funding
from features.funding.processed import Processed
from features.runtime.service import Service
from features.strategy.dev_create import CreateSignals, create_route
from launchpads.create import decode
from tests.helpers import Notices, address
from tests.test_create_decoders import pump, launchlab
from tests.test_native_buy import sample as native_sample
from trade_execution.executor import Executor


def launch(raw=None,*,processed=False):
    raw=raw or pump()
    raw['blockTime']=int(time.time())
    if processed:raw['_stream']={'commitment':'processed','live':True,'received_at':time.time()}
    tx=Tx(raw)
    return raw,tx,decode(tx)[0]


def fund(store,item,*,slot=None,stamp=None):
    now=int(time.time())
    funding=Funding('deposit-'+item.user+'-'+str(slot or item.slot-1), 'deposit',item.user,address(),
                    'cex:test',10**9,slot or item.slot-1,stamp or now-10)
    store.admit(funding,3600,time.time())
    return funding


@pytest.mark.parametrize('raw,kind',[
    (lambda:pump(v2=False),'pump_native_curve'),
    (lambda:pump(),'pump_native_curve'),
    (lambda:pump(quote=address()),'sol_to_pump_curve'),
    (lambda:launchlab(),'stonk_native_curve'),
    (lambda:launchlab(quote=address()),'sol_to_stonk_curve'),
    (lambda:pump(cpi=True),'pump_native_curve'),
    (lambda:launchlab(cpi=True),'stonk_native_curve'),
])
def test_pure_create_has_executable_route_without_dev_buy(raw,kind):
    _,tx,item=launch(raw())
    route=create_route(item,tx)
    assert route.kind==kind and route.trade.wallet==item.user
    assert route.trade.side=='create' and route.trade.quote==0
    request=route.request()
    assert request['mint']==item.mint and request['minSlot']==item.slot
    assert request['quoteMint']==item.quote_mint
    assert request['tokenProgram']==item.base_token_program


def test_metadata_creator_cannot_borrow_hotlist_or_unsigned_payer(config,store):
    _,tx,item=launch()
    assert item.creator!=item.user and item.creator not in tx.signers
    fund(store,replace(item,user=item.creator))
    signals=CreateSignals(store,config,started=0)
    assert signals.prepare(item,tx,True) is None
    assert item.creator in store.hotlist(time.time())
    tx.signers.remove(item.user)
    assert create_route(item,tx) is None


def test_one_hotlisted_dev_triggers_without_votes_and_consumes_once(config,store):
    _,tx,item=launch();fund(store,item)
    signals=CreateSignals(store,replace(config,n=99),started=0)
    route=signals.prepare(item,tx,True)
    assert route and not store.hotlist(time.time())
    oid=signals.reserve(route)
    assert oid and store.order(oid)['amount']==str(config.buy_amount)
    assert store.rows('trading','SELECT * FROM votes')==[]
    assert store.rows('trading','SELECT * FROM order_sources')==[
        {'order_id':oid,'signature':item.signature,'slot':item.slot}]
    assert signals.prepare(item,tx,True) is None
    assert CreateSignals(store,config,started=0).prepare(item,tx,True) is None


@pytest.mark.parametrize('case',['not-hotlisted','expired','same-slot','stale','not-live','prior-launch'])
def test_create_entry_rejections(config,store,case):
    _,tx,item=launch()
    if case=='expired':fund(store,item,stamp=int(time.time())-4000)
    elif case=='same-slot':fund(store,item,slot=item.slot)
    elif case!='not-hotlisted':fund(store,item)
    if case=='prior-launch':
        previous=replace(item,event='previous',signature='previous',slot=item.slot-2,time=item.time-20)
        store.record_creates([previous])
    if case=='stale':
        item=replace(item,time=item.time-config.signal_age-1)
        with store.db('funding') as db:db.execute('UPDATE funding SET time=?',(item.time-1,))
    signals=CreateSignals(store,config,started=0)
    assert signals.prepare(item,tx,case!='not-live') is None
    assert store.rows('trading','SELECT * FROM orders')==[]
    if case in ('stale','not-live','prior-launch'):
        assert store.hotlist(time.time())=={}


def test_failed_order_or_new_deposit_does_not_rearm_same_launch(config,store):
    _,tx,item=launch();fund(store,item);signals=CreateSignals(store,config,started=0)
    route=signals.prepare(item,tx,True);oid=signals.reserve(route)
    store.fail_order(oid,'simulation-failed')
    assert signals.prepare(item,tx,True) is None
    fund(store,item,slot=item.slot+1)
    assert signals.prepare(item,tx,True) is None
    second=replace(item,event='second',signature='second',slot=item.slot+2)
    other=Tx({**tx.raw,'slot':second.slot,'transaction':{**tx.raw['transaction'],'signatures':['second']}})
    assert signals.prepare(second,other,True) is None  # Previous successful launch.


def test_processed_invalid_create_restores_only_its_consumed_funding(config,store):
    raw,tx,item=launch(processed=True);fund(store,item)
    cfg=replace(config,hotlist_commitment='processed')
    with store.db('funding') as db:Processed.record(db,raw,time.time())
    signals=CreateSignals(store,cfg,started=0)
    assert signals.prepare(item,tx,True)
    Processed(store).invalidate(item.signature,'failed-or-different-slot')
    signals.invalidate(item.signature)
    assert item.user in store.hotlist(time.time())
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'invalid'}]


def test_uncertain_processed_create_does_not_restore(config,store):
    raw,tx,item=launch(processed=True);fund(store,item)
    with store.db('funding') as db:Processed.record(db,raw,time.time())
    signals=CreateSignals(store,replace(config,hotlist_commitment='processed'),started=0)
    assert signals.prepare(item,tx,True)
    Processed(store).invalidate(item.signature,'confirmation-unavailable',uncertain=True)
    signals.invalidate(item.signature)
    assert store.hotlist(time.time())=={}


def test_dead_slot_and_confirmation_upgrade_keep_launch_evidence(config,store):
    raw,tx,item=launch(processed=True);fund(store,item)
    with store.db('funding') as db:Processed.record(db,raw,time.time())
    signals=CreateSignals(store,replace(config,hotlist_commitment='processed'),started=0)
    assert signals.prepare(item,tx,True)
    assert store.record_creates([item])==[item]
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'confirmed'}]
    Processed(store).dead_slot(item.slot)
    assert item.user in store.hotlist(time.time())


def test_invalid_old_create_cannot_replace_newer_hotlist_removal(config,store):
    _,tx,item=launch();fund(store,item)
    signals=CreateSignals(store,config,started=0);route=signals.prepare(item,tx,True)
    later=replace(route.trade,signature='later',slot=item.slot+2)
    store.remove_hotlist(later,'later-buy')
    store.invalidate(item.signature)
    assert store.hotlist(time.time())=={}
    assert store.rows('funding','SELECT signature FROM hotlist_removals')==[{'signature':'later'}]


class Gates:
    def __init__(self,allowed=True):self.allowed=allowed;self.calls=[]
    def observe_create(self,item):pass
    def observe(self,route):self.calls.append(route)
    def start(self,route,rpc):self.calls.append(route);return self.allowed
    def start_create(self,item,tx,route):self.calls.append(route);return self.allowed
    async def check(self,route):self.calls.append(route);return self.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('factory',[pump,launchlab])
async def test_service_create_simulates_once_without_dev_buy(config,store,factory):
    raw,tx,item=launch(factory());fund(store,item)
    service=Service(replace(config,n=99),store);service.signals.started=0
    service.notices=Notices();service.quote_builder=Gates();service.dev_holdings=Gates();service.market_cap=Gates()
    class RPC:
        calls=[]
        async def transaction(self,sig):return raw
        async def call(self,method,params):
            self.calls.append(method)
            assert method=='simulateTransaction'
            return {'value':{'err':None}}
    rpc=RPC()
    async def build(cfg,route,wallet):
        ix=transfer(TransferParams(from_pubkey=pubkey(wallet),to_pubkey=Keypair().pubkey(),lamports=cfg.buy_amount))
        message=MessageV0.try_compile(pubkey(wallet),[ix],[],Hash.default())
        wire=VersionedTransaction.populate(message,[Signature.default()])
        return {'transaction':base64.b64encode(bytes(wire)).decode(),'lastHeight':100,
                'wallet':wallet,'mint':route.trade.mint,'amount':str(cfg.buy_amount),
                'quotedOut':'1000','minOut':'980','quoteIn':'100'}
    service.executor=Executor(config,store,rpc,service.notices,service.priority,builder=build)
    store.enqueue([item.signature],live=True)
    await service.process({'signature':item.signature},rpc)
    await service.process({'signature':item.signature},rpc)
    assert [r['state'] for r in store.rows('trading','SELECT state FROM orders')]==['dry-simulated']
    assert rpc.calls==['simulateTransaction']
    assert not store.rows('trading','SELECT * FROM votes')
    assert not store.hotlist(time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize('gate',['dev'])
async def test_create_preserves_entry_risk_checks(config,store,gate):
    raw,tx,item=launch();fund(store,item)
    service=Service(config,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates(gate!='dev');service.market_cap=Gates(gate!='cap')
    class RPC:
        async def transaction(self,sig):return raw
    store.enqueue([item.signature],live=True)
    await service.process({'signature':item.signature},RPC())
    assert store.rows('trading','SELECT * FROM orders')==[]
    assert not store.hotlist(time.time())


@pytest.mark.asyncio
async def test_ordinary_hotlist_buy_does_not_trigger_entry(config,store):
    from trade_execution.native import decode_native
    raw=native_sample();raw['blockTime']=int(time.time());tx=Tx(raw)
    route=decode_native(tx)[0]
    item=replace(decode(Tx(pump()))[0],user=route.trade.wallet,slot=tx.slot)
    fund(store,item)
    service=Service(replace(config,n=1),store);service.signals.started=0
    service.notices=Notices();service.quote_builder=Gates()
    class RPC:
        async def transaction(self,sig):return raw
    store.enqueue([tx.signature],live=True)
    await service.process({'signature':tx.signature},RPC())
    assert not store.rows('trading','SELECT * FROM orders')
    assert not store.rows('trading','SELECT * FROM votes')
    assert route.trade.wallet in store.hotlist(time.time())


@pytest.mark.asyncio
async def test_processed_create_uses_stream_payload_and_never_buys_confirmed_replay(config,store):
    raw,tx,item=launch(processed=True);raw['blockTime']=None
    fund(store,item)
    cfg=replace(config,hotlist_commitment='processed')
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates();service.market_cap=Gates()
    calls=[]
    class Engine:
        async def buy(self,oid,route):calls.append(route)
    service.executor=Engine()
    class NoRpc:
        async def call(self,*args):raise AssertionError('complete streamed create needs no RPC')
        async def transaction(self,*args):raise AssertionError('complete streamed create needs no RPC')
    store.stream_transaction(raw)
    store.mark_live(item.signature)
    await service.process({'signature':item.signature},NoRpc())
    assert len(calls)==1
    Processed(store).confirmed(item.signature,'confirmed')
    raw.pop('_stream');raw['blockTime']=int(time.time())
    class Confirmed:
        async def transaction(self,*args):return raw
    await service.process({'signature':item.signature},Confirmed())
    assert len(calls)==1
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'confirmed'}]


@pytest.mark.asyncio
async def test_rejected_create_order_keeps_consumption(config,store):
    raw,tx,item=launch();fund(store,item)
    service=Service(config,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates();service.market_cap=Gates()
    calls=[]
    class Engine:
        async def buy(self,oid,route):
            calls.append(oid);store.fail_order(oid,'simulation-rejected')
    service.executor=Engine()
    class RPC:
        async def transaction(self,*args):return raw
    store.enqueue([item.signature],live=True)
    await service.process({'signature':item.signature},RPC())
    await service.process({'signature':item.signature},RPC())
    assert len(calls)==1 and not store.hotlist(time.time())
