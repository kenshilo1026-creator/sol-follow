import asyncio
from dataclasses import replace
import time

import pytest
from features.funding.create_feed import CreateFeed
from features.funding.processed import Processed
from features.runtime.service import Service
from features.strategy.dev_sell import DevSell
from features.strategy.market_feed import MarketFeed
from tests.helpers import Notices
from tests.test_dev_create import launch, fund, Gates


def notify(feed,item,*,signature=None,slot=None,logs=None,err=None):
    feed.notification({'params':{'subscription':1,'result':{'context':{'slot':slot or item.slot},
        'value':{'signature':signature or item.signature,'err':err,
                 'logs':logs if logs is not None else ['Program log: Instruction: CreateV2']}}}}, {item.user:1})


class RPC:
    def __init__(self,raw):self.raw=raw;self.calls=[]
    async def transaction(self,sig,commitment='confirmed'):
        self.calls.append((sig,commitment));return self.raw


def setup(config,store):
    raw,tx,item=launch();fund(store,item)
    cfg=replace(config,hotlist_commitment='processed',feed_mode='alchemy_grpc')
    rpc=RPC(raw);feed=CreateFeed(cfg,store,rpc,Notices())
    return raw,item,cfg,rpc,feed


@pytest.mark.asyncio
async def test_backup_alone_arms_dev_before_one_buy_in_processed_mode(config,store):
    raw,item,cfg,rpc,feed=setup(config,store)
    feed.refresh();assert item.user in feed.addresses and not feed.funding_sources
    notify(feed,item);notify(feed,item)
    await feed.once()
    assert rpc.calls==[(item.signature,'confirmed')]
    assert Processed(store).create_backup(item.signature,item.slot)
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates()
    worker=DevSell(cfg,store,None,Notices(),None)
    market=MarketFeed(cfg,store,None,Notices(),worker.watch_addresses,worker.market_event,worker.subscribed)
    service.market_feed=market
    # ACKs are asynchronous; buy must wait while both required addresses arm.
    async def acknowledge():
        while not worker.watches:await asyncio.sleep(0)
        for f in market.feeds:f.subscribed=set(worker.watches);f.connected=True
    ack=asyncio.create_task(acknowledge());buys=[]
    class Engine:
        async def buy(self,oid,route):
            assert item.user in worker.watches
            worker.market_event(item.user,'early-dev-sale',item.slot+1)
            assert store.rows('trading','SELECT signature FROM dev_sell_events')==[{'signature':'early-dev-sale'}]
            assert set(worker.watches)<=market.feeds[0].subscribed
            from trade_execution.executor import Executor
            assert Executor(cfg,store,None,Notices(),service.priority).fresh(route,oid)
            buys.append(oid)
    service.executor=Engine()
    await service.process({'signature':item.signature},rpc)
    await ack
    # A second source/repeated receipt cannot buy again.
    await service.process({'signature':item.signature},rpc)
    assert len(buys)==1
    assert not store.hotlist(time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['stale','wrong-slot','wrong-signature','failed','not-create','ordinary-log','no-hotlist','dead-slot'])
async def test_backup_never_promotes_unverified_or_stale_receipts(config,store,case):
    raw,item,cfg,rpc,feed=setup(config,store)
    if case=='stale':raw['blockTime']=int(time.time())-cfg.signal_age-2
    if case=='wrong-slot':raw['slot']+=1
    if case=='wrong-signature':raw['transaction']['signatures']=['wrong']
    if case=='failed':raw['meta']['err']={'InstructionError':[0,'Custom']}
    if case=='not-create':raw['transaction']['message']['instructions']=[];raw['meta']['innerInstructions']=[]
    if case=='no-hotlist':
        with store.db('funding') as db:db.execute('DELETE FROM hotlist')
    if case=='dead-slot':Processed(store).dead_slot(item.slot)
    notify(feed,item,logs=['Program log: Instruction: Buy'] if case=='ordinary-log' else None)
    await feed.once()
    assert not Processed(store).create_backup(item.signature,item.slot)
    assert store.rows('funding','SELECT * FROM jobs')==[]


@pytest.mark.asyncio
async def test_delayed_receipt_does_not_refresh_live_window(config,store):
    raw,item,cfg,rpc,feed=setup(config,store)
    notify(feed,item);rpc.raw=None
    await feed.once()
    feed.pending[item.signature]['seen']=time.time()-cfg.signal_age-1
    rpc.raw=raw
    await feed.once()
    assert not feed.pending and len(rpc.calls)==1
    assert not Processed(store).create_backup(item.signature)


@pytest.mark.asyncio
async def test_missing_dev_subscription_prevents_buy(config,store):
    raw,item,cfg,rpc,feed=setup(config,store);notify(feed,item);await feed.once()
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates()
    worker=DevSell(cfg,store,None,Notices(),None)
    market=MarketFeed(cfg,store,None,Notices(),worker.watch_addresses,worker.market_event,worker.subscribed)
    class Unavailable:
        async def arm(self,required):return await market.arm(required,timeout=0)
    service.market_feed=Unavailable()
    class Engine:
        async def buy(self,*args):raise AssertionError('unprotected buy')
    service.executor=Engine()
    await service.process({'signature':item.signature},rpc)
    assert item.user in worker.watches
    assert store.rows('trading','SELECT reason FROM orders')[0]['reason']=='dev-sell-monitor-unavailable'


@pytest.mark.asyncio
async def test_confirmed_backup_recovers_nonlive_grpc_but_not_invalid(config,store):
    raw,item,cfg,rpc,feed=setup(config,store)
    raw['_stream']={'commitment':'processed','live':False}
    store.stream_transaction(raw)
    notify(feed,item);await feed.once()
    proof=Processed(store)
    assert proof.create_backup(item.signature,item.slot) and proof.usable(item.signature,slot=item.slot)
    proof.confirmed(item.signature,'finalized')
    assert proof.create_backup(item.signature,item.slot)
    proof.invalidate(item.signature,'failed');feed.commit(raw,{'seen':time.time()})
    assert not proof.create_backup(item.signature)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_commitment',['processed','confirmed'])
async def test_late_grpc_duplicate_preserves_verified_backup(config,store,late_commitment):
    raw,item,cfg,rpc,feed=setup(config,store);notify(feed,item);await feed.once()
    store.stream_transaction({**raw,'_stream':{'commitment':late_commitment,'live':True}})
    assert store.stream_payload(item.signature)['_stream']['commitment']=='confirmed'
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates()
    buys=[]
    class Engine:
        async def buy(self,*args):buys.append(args)
    service.executor=Engine()
    await service.process({'signature':item.signature},rpc)
    assert len(buys)==1
    store.stream_transaction({**raw,'_stream':{'commitment':late_commitment,'live':True}})
    assert store.due(1,time.time()+1)==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('gate',['aged-funding','restart','stale-chain'])
async def test_backup_preserves_entry_gates(config,store,gate):
    raw,item,cfg,rpc,feed=setup(config,store);notify(feed,item);await feed.once()
    if gate=='aged-funding':cfg=replace(cfg,min_funding_age_seconds=600)
    if gate=='restart':store.live_receipts.clear()
    if gate=='stale-chain':
        payload=store.stream_payload(item.signature)
        payload['blockTime']=int(time.time())-cfg.signal_age-2
        import json
        with store.db('funding') as db:db.execute('UPDATE stream_payloads SET raw=?',(json.dumps(payload),))
    service=Service(cfg,store);service.signals.started=0;service.notices=Notices()
    service.quote_builder=Gates();service.dev_holdings=Gates()
    class Engine:
        async def buy(self,*args):raise AssertionError('entry gate bypassed')
    service.executor=Engine()
    await service.process({'signature':item.signature},rpc)
    assert store.rows('trading','SELECT * FROM orders')==[]
