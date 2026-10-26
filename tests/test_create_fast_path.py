import asyncio
from dataclasses import replace
import json
import time
import pytest
from solders.keypair import Keypair
from chain_common.rpc import Priority
from features.funding.processed import Processed
from features.audit.feed_trace import observed,compare
from features.audit.dev_audit import report
from features.audit.writer import record
from trade_execution.executor import Executor
from tests.helpers import Notices
from tests.test_pump_buy import setup_order,RPC


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['ready','unavailable','fork','expired','dev-rejected','confirmed'])
async def test_prepare_before_ack_but_never_send_without_valid_coverage(config,store,outcome):
    key=Keypair()
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    cfg=replace(cfg,hotlist_commitment='processed')
    route=replace(route,trade=replace(route.trade,side='create'))
    sig=route.trade.signature
    with store.db('funding') as db:
        db.execute('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?)',
                   (sig,route.trade.slot,time.time(),1,'pending',time.time(),''))
    with store.db('trading') as db:db.execute('INSERT INTO order_sources VALUES (?,?,?)',(oid,sig,route.trade.slot))
    built=asyncio.Event();simulated=asyncio.Event();release=asyncio.Event()
    async def build(*args):
        built.set()
        return await builder(*args)
    class Public(RPC):
        async def call(self,method,params):
            if method=='simulateTransaction':simulated.set()
            return await super().call(method,params)
    rpc=Public(store,oid)
    async def coverage():
        await release.wait()
        return outcome!='unavailable'
    ready=asyncio.create_task(coverage())
    engine=Executor(cfg,store,rpc,Notices(),Priority(),build)
    task=asyncio.create_task(engine.buy(oid,route,readiness=ready))
    try:
        await asyncio.wait_for(built.wait(),2)
        await asyncio.wait_for(simulated.wait(),2)
        assert not task.done() and not ready.done()
        assert store.order(oid)['signature'] is None
        assert rpc.calls==['simulateTransaction']
        if outcome=='fork':Processed(store).dead_slot(route.trade.slot)
        elif outcome=='confirmed':Processed(store).confirmed(sig,'confirmed')
        elif outcome=='expired':engine.config=replace(cfg,signal_age=-1)
        elif outcome=='dev-rejected':
            with store.db('trading') as db:
                db.execute('INSERT INTO token_dev_holdings VALUES (?,?,?,?,?,?,?)',
                    (route.trade.mint,sig,route.trade.wallet,route.trade.slot,time.time(),'blocked','{}'))
        release.set()
        await asyncio.wait_for(task,2)
    finally:
        for pending in (task,ready):
            if not pending.done():pending.cancel()
        await asyncio.gather(task,ready,return_exceptions=True)
    expected={'unavailable':'dev-sell-monitor-unavailable','fork':'source-proof-unusable',
              'expired':'signal-expired','dev-rejected':'dev-entry-not-permitted'}
    order=store.order(oid)
    if outcome in expected:
        assert order['state']=='failed' and order['reason']==expected[outcome]
        assert order['signature'] is None and 'sendTransaction' not in rpc.calls
    else:
        assert order['state']=='submitted' and rpc.calls[-1]=='sendTransaction'
    detail=json.loads(store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]['detail'])
    assert 'built_ms' in detail['timing_ms'] and 'simulated_ms' in detail['timing_ms']


def test_feed_first_observations_survive_later_backup_and_report_by_wallet(config,store):
    now=time.time();sig='source';wallet='wallet'
    record(store,'dev-create','candidate','hotlisted-dev-create',wallet=wallet,signature=sig)
    observed(store,sig,'grpc-processed','received',100,now,live=False,normalized=True)
    observed(store,sig,'create-confirmed','received',100,now+1)
    observed(store,sig,'grpc-processed','received',100,now+2,live=True)
    result=report(config.data,'wallet',wallet=wallet)
    assert len(result['feed_events'])==2
    order=result['feed_comparison'][0]
    assert order['first_observed']=='grpc-processed' and order['confirmed_minus_grpc_ms']==1000
    assert result['feed_events'][0]['detail']['live'] is False
    assert compare(result['feed_events'][1:])[0]['first_observed']=='insufficient-evidence'


@pytest.mark.asyncio
async def test_service_cancels_monitor_task_after_precheck_rejection(config,store):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(config,store)
    started=asyncio.Event();cancelled=asyncio.Event()
    class Market:
        async def arm(self,required):
            started.set()
            try:await asyncio.Future()
            finally:cancelled.set()
    class Engine:
        async def buy(self,oid,route,*,readiness):
            await asyncio.sleep(0)
            assert started.is_set()
            store.fail_order(oid,'test-precheck-rejected')
    case.service.market_feed=Market();case.service.executor=Engine()
    await case.service.process(case.row,case.rpc)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_maintenance_rechecks_priority_between_checkpoint_databases(config,store,monkeypatch):
    from contextlib import contextmanager
    import features.database.maintenance as module
    priority=Priority();names=[];original=store.db
    monkeypatch.setattr(module,'batch',lambda *args:0)
    @contextmanager
    def db(name):
        names.append(name)
        with original(name) as conn:
            priority.active=1
            yield conn
    monkeypatch.setattr(store,'db',db)
    await module.Maintenance(config,store,priority,Notices()).pass_once()
    assert names==['audit']


@pytest.mark.asyncio
async def test_cached_processed_create_can_upgrade_before_decoding(config,store,monkeypatch):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(config,store)
    original=store.stream_payload
    def payload(signature):
        raw=original(signature)
        Processed(store).confirmed(signature,'confirmed')
        return raw
    monkeypatch.setattr(store,'stream_payload',payload)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1


def test_stale_block_metadata_disconnects_instead_of_draining_old_stream(config,store,monkeypatch):
    from features.funding.grpc_feed import HotlistFeed
    from chain_common.yellowstone import geyser_pb2 as pb
    notices=Notices();feed=HotlistFeed(config,store,None,notices,None)
    monkeypatch.setattr(time,'time',lambda:10000)
    update=pb.SubscribeUpdate()
    update.block_meta.slot=461555503
    update.block_meta.block_time.timestamp=9400
    with pytest.raises(RuntimeError,match='grpc-stale-block-time'):feed.check_freshness(update)
    assert notices.items[-1][0][1]['lag_seconds']==600
    update.block_meta.block_time.timestamp=10000
    feed.check_freshness(update)


def test_nonlive_grpc_does_not_create_http_recovery_jobs(config,store,monkeypatch):
    from features.funding.grpc_feed import HotlistFeed
    from tests.test_streaming import wire
    from tests.test_native_buy import sample
    from chain_common.transaction import Unsupported
    raw=sample();feed=HotlistFeed(config,store,None,Notices(),None)
    feed.accept(wire(raw))
    def broken(*args):raise Unsupported('test-normalization-failed')
    monkeypatch.setattr('features.funding.grpc_feed.normalize',broken)
    feed.accept(wire(raw))
    assert feed.dropped==2 and feed.added==0
    assert not store.rows('funding','SELECT * FROM jobs')
