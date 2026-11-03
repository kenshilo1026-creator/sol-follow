import json
from pathlib import Path
from chain_common.transaction import Tx
from launchpads import stonk,ENABLED
from launchpads.create import inspect
from launchpads.factories import lookup
from trade_execution.route import decode_all


def test_happa_factory_is_registered_disabled_and_not_stonk():
    raw=json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    tx=Tx(raw)
    ix=tx.top[2]
    factory=lookup(ix['programId'],ix['accounts'][3])
    assert factory['enable'] is False
    assert factory['example_signature']==tx.signature
    assert factory['example_mint']==ix['accounts'][6]
    assert factory['platform']!=stonk.PLATFORM
    assert factory['name'] not in ENABLED
    result=inspect(tx)
    assert len(result.creates)==1 and not result.rejected
    assert result.creates[0].launchpad==factory['name']
    assert result.creates[0].flags['platform']==factory['platform']
    assert not decode_all(tx)


def test_registry_does_not_match_program_alone_or_replace_stonk():
    assert lookup(stonk.PROGRAM,stonk.PLATFORM)['enable'] is True
    assert lookup('11111111111111111111111111111111',
                  'FTFNRRsEjyE175KkQFco4txrDpcsh5suZvtPhVFhmZ1C') is None


import copy
import time
from dataclasses import replace
from types import SimpleNamespace
import pytest
from launchpads.factories import FACTORIES,enabled
from features.runtime.service import Service
from features.funding.decoder import Funding
from features.funding.processed import Processed
from features.strategy.dev_create import create_route
from tests.helpers import address,Notices
from tests.test_create_decoders import pump,launchlab


def disabled_raw(kind,monkeypatch):
    if kind=='happa':
        return json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    raw=pump() if kind=='pump' else launchlab()
    tx=Tx(raw);item=inspect(tx).creates[0]
    row=next(r for r in FACTORIES.values() if r['name']==item.launchpad)
    monkeypatch.setitem(row,'enable',False)
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['pump','stonk','happa'])
@pytest.mark.parametrize('processed',[False,True])
async def test_disabled_factory_consumes_without_any_buy_or_warm(config,store,monkeypatch,kind,processed):
    raw=disabled_raw(kind,monkeypatch);now=int(time.time());raw['blockTime']=now
    tx=Tx(raw);item=inspect(tx).creates[0]
    assert not enabled(item) and create_route(item,tx) is None
    # Funding is intentionally younger than the buy age gate: still consumed.
    funding=Funding('disabled-fund','deposit',item.user,address(),'cex:test',10**9,item.slot-1,now-10)
    store.admit(funding,3600,time.time())
    cfg=replace(config,hotlist_commitment='processed' if processed else 'confirmed')
    service=Service(cfg,store);service.notices=Notices()
    def forbidden(*args,**kwargs):raise AssertionError('disabled factory reached trading preparation')
    service.executor=SimpleNamespace(buy=forbidden)
    service.dev_holdings=SimpleNamespace(start_create=forbidden)
    service.quote_builder=SimpleNamespace(observe_create=forbidden,observe=lambda *args:None)
    class RPC:
        async def transaction(self,*args,**kwargs):return raw
        async def call(self,*args,**kwargs):raise AssertionError('unexpected RPC')
    if processed:
        raw['_stream']={'commitment':'processed','live':True}
        store.stream_transaction(raw)
    else:store.enqueue([item.signature],live=True)
    store.mark_live(item.signature)
    await service.process({'signature':item.signature},RPC())
    assert item.user not in store.hotlist(time.time())
    removal=store.rows('funding','SELECT * FROM hotlist_removals WHERE wallet=?',(item.user,))[0]
    assert removal['reason']=='dev-create-disabled' and removal['signature']==item.signature
    assert not store.rows('trading','SELECT * FROM orders')
    audit=store.rows('audit',"SELECT * FROM decisions WHERE reason='factory-disabled'")
    assert audit and json.loads(audit[0]['detail'])['eligibility_consumed']
    await service.process({'signature':item.signature},RPC())
    assert not store.rows('trading','SELECT * FROM orders')
    # Replays, even after restart, cannot consume a later deposit.
    newer=replace(funding,event='newer-fund',signature='newer',slot=item.slot+1)
    store.admit(newer,3600,time.time())
    await service.process({'signature':item.signature},RPC())
    assert store.hotlist(time.time())[item.user]['slot']==item.slot+1
    # A proved rollback restores eligibility, including the normal processed path.
    if processed:Processed(store).dead_slot(item.slot)
    else:store.invalidate(item.signature)
    assert item.user in store.hotlist(time.time())
    assert not store.rows('funding','SELECT * FROM hotlist_removals WHERE wallet=?',(item.user,))


def test_disabled_create_requires_complete_abi_and_signing_launcher(config,store):
    from features.strategy.dev_create import CreateSignals
    raw=json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    raw['blockTime']=int(time.time());tx=Tx(raw);item=inspect(tx).creates[0]
    store.admit(Funding('fund','deposit',item.user,address(),'cex:test',10**9,item.slot-1,item.time-10),3600,time.time())
    damaged=copy.deepcopy(raw);damaged['transaction']['message']['instructions'][2]['accounts'][4]=address()
    assert not inspect(Tx(damaged)).creates
    tx.signers.discard(item.user)
    assert CreateSignals(store,config).prepare(item,tx,True) is None
    assert item.user in store.hotlist(time.time())
    failed=copy.deepcopy(raw);failed['meta']['err']={'InstructionError':[2,{'Custom':1}]}
    with pytest.raises(ValueError):Tx(failed)


def test_disabled_factory_create_counts_as_prior_launch():
    from features.funding.activity_filter import previous_launch
    raw=json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    item=inspect(Tx(raw)).creates[0]
    assert previous_launch(raw,item.user,raw['blockTime'])['launchpad']==item.launchpad


@pytest.mark.asyncio
async def test_disabled_factory_confirmed_backup_consumes_in_processed_mode(config,store):
    from features.funding.create_feed import CreateFeed
    from tests.test_create_backup import notify,RPC
    raw=json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    raw['blockTime']=int(time.time());item=inspect(Tx(raw)).creates[0]
    funding=Funding('backup-fund','deposit',item.user,address(),'cex:test',10**9,item.slot-1,item.time-10)
    store.admit(funding,3600,time.time())
    cfg=replace(config,hotlist_commitment='processed',feed_mode='alchemy_grpc')
    rpc=RPC(raw);feed=CreateFeed(cfg,store,rpc,Notices())
    notify(feed,item,logs=['Program log: Instruction: InitializeV2'])
    await feed.once()
    assert Processed(store).create_backup(item.signature,item.slot)
    service=Service(cfg,store);service.notices=Notices()
    def forbidden(*args,**kwargs):raise AssertionError('disabled backup reached buy')
    service.executor=SimpleNamespace(buy=forbidden)
    await service.process({'signature':item.signature},rpc)
    assert item.user not in store.hotlist(time.time())
    assert not store.rows('trading','SELECT * FROM orders')
    store.invalidate(item.signature)
    assert store.hotlist(time.time())[item.user]['event']==funding.event


def test_disabled_create_without_eligible_funding_does_not_write_removal(config,store):
    from features.strategy.dev_create import CreateSignals
    raw=json.loads((Path(__file__).parent/'fixtures/create_happa_disabled.json').read_text())
    raw['blockTime']=int(time.time());tx=Tx(raw);item=inspect(tx).creates[0]
    signals=CreateSignals(store,replace(config,hotlist_commitment='confirmed'))
    assert signals.prepare(item,tx,True) is None
    assert not store.rows('funding','SELECT * FROM hotlist_removals')


@pytest.mark.parametrize('kind',['pump','stonk','happa'])
def test_disabled_factory_not_warmed_from_persisted_creates(config,store,monkeypatch,kind):
    from trade_execution.cache import CachedBuilder
    raw=disabled_raw(kind,monkeypatch);item=inspect(Tx(raw)).creates[0]
    builder=CachedBuilder(config,store,Notices(),SimpleNamespace(active=0))
    builder.observe_create(item)
    assert not builder.queue and not builder.shared_queue
