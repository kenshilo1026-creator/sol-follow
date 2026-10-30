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
@pytest.mark.parametrize('outcome',['ready','unavailable','fork','expired','dev-rejected','confirmed','disconnected'])
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
    ready.coverage_check=lambda:outcome!='disconnected'
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
    expected={'disconnected':'dev-sell-monitor-unavailable','unavailable':'dev-sell-monitor-unavailable','fork':'source-proof-unusable',
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


@pytest.mark.parametrize('source_age',[.1,11.,-2.])
def test_stale_diagnostic_uses_rejected_message_timestamp(config,store,monkeypatch,source_age):
    from features.funding.grpc_feed import HotlistFeed
    from chain_common.yellowstone import geyser_pb2 as pb
    from share_common.notify import message
    notices=Notices();feed=HotlistFeed(config,store,None,notices,None)
    monkeypatch.setattr(time,'time',lambda:10000.)
    feed.last_source_age=999  # Previous accepted update must not be attributed here.
    feed.delivery.add('block_meta_local_accept',.012)
    feed.delivery.add('transaction_local_accept',.025)
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=9900
    stamp=10000-source_age
    update.created_at.seconds=int(stamp)
    update.created_at.nanos=round((stamp-int(stamp))*1e9)
    with pytest.raises(RuntimeError,match='grpc-stale-block-time'):feed.check_freshness(update)
    kind,detail=notices.items[-1][0]
    assert detail['source_age_s']==source_age
    assert detail['source_block_lag_s']==round(100-source_age,3)
    assert detail['block_meta_accept_p95_ms']==12
    assert detail['transaction_accept_p95_ms']==25
    text=message(kind,detail)
    assert f'本機與上游訊息時間差: {source_age} 秒' in text
    assert '超時買入訊號仍會跳過' in text


def test_stale_alert_without_source_clock_does_not_invent_transport_latency():
    from share_common.notify import message
    text=message('hotlist stream stale; reconnecting',{
        'slot':100,'lag_seconds':11.101,'maximum_seconds':10,'stale_for_seconds':3.033})
    assert '上游訊息時間: 未提供' in text
    assert '本機與上游訊息時間差:' not in text


def test_stale_alert_includes_current_scheduler_stall_before_watchdog_resumes(config,store,monkeypatch):
    from features.funding.grpc_feed import HotlistFeed
    from chain_common.yellowstone import geyser_pb2 as pb
    from share_common.notify import message
    notices=Notices();feed=HotlistFeed(config,store,None,notices,None)
    monkeypatch.setattr(time,'time',lambda:10000.)
    monkeypatch.setattr(time,'monotonic',lambda:100.)
    feed.loop_due=90.
    feed.loop_delays.extend([(0.,30.),(99.,.01)])
    feed.read_ready_at=99.5
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=9900
    with pytest.raises(RuntimeError,match='grpc-stale-block-time'):feed.check_freshness(update)
    kind,detail=notices.items[-1][0]
    assert detail['event_loop_lag_max_ms']==10000
    assert detail['read_ready_wait_ms']==500
    assert '近 60 秒事件迴圈最大延遲: 10000.0 ms' in message(kind,detail)


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


@pytest.mark.parametrize('lag',[2.085,2.139,3,10])
def test_stream_jitter_does_not_relax_trade_deadline(config,store,monkeypatch,lag):
    from features.funding.grpc_feed import HotlistFeed
    from chain_common.yellowstone import geyser_pb2 as pb
    notices=Notices()
    feed=HotlistFeed(replace(config,signal_age=2),store,None,notices,None)
    now=10000+lag
    monkeypatch.setattr(time,'time',lambda:now)
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=10000
    feed.check_freshness(update)
    feed.accept(update)
    assert not notices.items
    assert feed.stale_since is None
    assert not feed.live_slot(101,now)  # Still cannot buy an expired signal.


def test_stream_staleness_requires_persistence_and_resets_on_recovery(config,store,monkeypatch):
    from features.funding.grpc_feed import HotlistFeed
    from chain_common.yellowstone import geyser_pb2 as pb
    feed=HotlistFeed(replace(config,signal_age=2),store,None,Notices(),None)
    clock=[10000.,0.]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    monkeypatch.setattr(time,'monotonic',lambda:clock[1])
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=9989
    feed.check_freshness(update)
    clock[1]=2.9
    feed.check_freshness(update)
    update.block_meta.block_time.timestamp=9999
    feed.check_freshness(update)
    assert feed.stale_since is None
    update.block_meta.block_time.timestamp=9989
    clock[1]=3
    feed.check_freshness(update)
    clock[1]=5.9
    feed.check_freshness(update)
    clock[1]=6
    with pytest.raises(RuntimeError,match='grpc-stale-block-time'):
        feed.check_freshness(update)


@pytest.mark.asyncio
async def test_stream_stale_timer_resets_on_reconnect(config,store):
    from features.funding.grpc_feed import HotlistFeed
    class Rpc:
        async def call(self,*args):return 100
    feed=HotlistFeed(config,store,Rpc(),Notices(),None)
    feed.stale_since=123
    await feed.resume_slot(set())
    assert feed.stale_since is None


@pytest.mark.parametrize('message,expected',[
    ('grpc-stale-block-time','grpc-stale-block-time'),
    ('grpc-ended','grpc-ended'),
    ('grpc-idle-timeout','grpc-idle-timeout'),
    ('grpc-write-timeout','grpc-write-timeout'),
    ('https://example.invalid/private-key','local-error'),
    ('grpc-ended private-key','local-error'),
])
def test_grpc_failure_reason_never_exposes_arbitrary_messages(message,expected):
    from features.funding.grpc_feed import failure_reason
    assert failure_reason(RuntimeError(message))==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected',[
    ('grpc-stale-block-time',[1,2,4,5,5,5,5,5]),
    ('grpc-ended',[1,2,4,8,16,32,60,60]),
    ('transport',[1,2,4,8,16,32,60,60]),
    ('long-lived',[1,2,4,8,16,32,60,1]),
])
async def test_grpc_retry_reports_actual_wait_and_caps_only_stale(config,store,monkeypatch,reason,expected):
    import grpc
    from features.funding import grpc_feed
    feed=grpc_feed.HotlistFeed(config,store,None,Notices(),None)
    waits=[]; reports=[]; clock=[0.]
    class Channel:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class RateLimited(grpc.RpcError):
        def code(self):return grpc.StatusCode.RESOURCE_EXHAUSTED
    async def resume(*args):return None
    async def connection(*args):
        if reason=='long-lived' and len(waits)==7:clock[0]+=31
        if reason=='transport':raise RateLimited()
        raise RuntimeError('grpc-ended' if reason=='long-lived' else reason)
    async def sleep(delay):
        waits.append(delay)
        assert reports[-1]['retry_s']==delay
        if len(waits)==len(expected):raise asyncio.CancelledError()
        clock[0]+=delay
    monkeypatch.setattr(feed,'addresses',lambda:{'wallet'})
    monkeypatch.setattr(feed,'resume_slot',resume)
    monkeypatch.setattr(feed,'connection',connection)
    monkeypatch.setattr(feed.health,'failure',lambda kind,detail:reports.append(detail))
    monkeypatch.setattr(grpc_feed,'channel_for',lambda cfg:Channel())
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):await feed.run()
    assert waits==expected
    assert not feed.connected
    assert feed.reconnects==len(expected)
    if reason=='transport':
        assert reports[-1]['status']=='RESOURCE_EXHAUSTED'
        assert reports[-1]['reason']=='grpc-transport-error'


@pytest.mark.asyncio
@pytest.mark.parametrize('launchpad',['pump','stonk'])
@pytest.mark.parametrize('non_sol',[False,True])
async def test_create_executes_before_funding_history_and_retry_does_not_buy_twice(config,store,monkeypatch,launchpad,non_sol):
    from tests.create_snipe_helpers import setup_create
    from features.funding.activity_filter import HistoryPending
    case=setup_create(config,store,processed=False,launchpad=launchpad,non_sol=non_sol)
    funding=object();attempts=[]
    monkeypatch.setattr('features.runtime.service.funding_decode',lambda *args:[funding])
    monkeypatch.setattr('features.runtime.service.funding_candidates',lambda *args:None)
    async def qualify(item,rpc):
        assert item is funding
        assert len(case.calls)==1  # build/execute is ahead of this slow RPC path
        attempts.append(item)
        if len(attempts)==1:raise HistoryPending('history-not-ready')
    monkeypatch.setattr(case.service,'qualify',qualify)
    with pytest.raises(HistoryPending,match='history-not-ready'):
        await case.service.process(case.row,case.rpc)
    await case.service.process(case.row,case.rpc)
    assert len(case.calls)==1 and len(attempts)==2
