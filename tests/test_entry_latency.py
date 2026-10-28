"""Offline regression checks for the live entry queue and durable execution."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from chain_common.rpc import Priority
from features.audit.writer import BufferedDecisions, record
from features.database.storage import Store
from features.runtime.service import Service
from tests.helpers import Notices
from tests.test_pump_buy import sample, RPC
from tests.test_single_buy_simulation import fixture
from trade_execution.cache import CachedBuilder
from trade_execution.executor import Executor


def live_payload(signature):
    raw=sample()
    raw['transaction']['signatures'][0]=signature
    raw['_stream']={'live':True,'commitment':'confirmed'}
    return raw


def test_reused_connections_rollback_and_reset_without_sharing_nested_transactions(store):
    with store.db('trading') as first:
        first.execute('PRAGMA synchronous=FULL')
        first.execute('BEGIN IMMEDIATE')
        first.execute("INSERT INTO events VALUES ('uncommitted',1,'sig')")
        with store.db('trading') as second:
            assert second is not first
            assert not second.execute("SELECT * FROM events WHERE event='uncommitted'").fetchall()
    assert not store.rows('trading',"SELECT * FROM events WHERE event='uncommitted'")
    with store.db('trading') as first:
        first.execute('PRAGMA synchronous=FULL')
        first.execute('PRAGMA busy_timeout=5')
        first.execute('PRAGMA query_only=ON')
    with store.db('trading') as again:
        assert again is first
        assert again.execute('PRAGMA synchronous').fetchone()[0]==1
        assert again.execute('PRAGMA busy_timeout').fetchone()[0]==50
        again.execute("INSERT INTO events VALUES ('committed',1,'sig')")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):again.execute('SELECT 1')


def test_job_counts_are_per_operation_with_reused_connections(store):
    assert store.enqueue(['a','b'])==2
    assert store.enqueue(['a','c'])==1
    store.save_cursor('source',{},['d','e'])
    store.job_result('d','done')
    store.save_cursor('source',{},['d','e','f'],revisit=True)
    assert store.new_jobs==6 and store.revisited_jobs==1


def test_fast_queue_keeps_durable_claim_backoff_and_restart_rules(store,config):
    now=time.time()
    store.enqueue(['history'],now-100)
    for sig in ['valid','backoff','active','done','no-payload']:
        store.stream_transaction(live_payload(sig))
    store.job_result('done','done')
    with store.db('funding') as db:
        db.execute("UPDATE jobs SET due=? WHERE signature='backoff'",(now+60,))
        db.execute("DELETE FROM stream_payloads WHERE signature='no-payload'")
    rows=store.due_live_stream(time.time(),excluded={'active'})
    assert [r['signature'] for r in rows]==['valid']
    assert store.due_live_stream(time.time())==[]
    assert store.due(1,time.time(),streamed=True,excluded={'active'})==[]
    restarted=Store(config.data)
    assert not restarted.live_stream_jobs and not restarted.is_live('valid',300)
    assert restarted.stream_payload('valid') is not None
    assert restarted.due_live_stream(time.time()+61)==[]
    assert {r['signature'] for r in restarted.due(10,time.time()+61,streamed=True)}=={'valid','backoff','active'}


def test_fast_queue_failure_does_not_remove_receipt_before_commit(store,monkeypatch):
    store.stream_transaction(live_payload('valid'))
    original=store.db
    @contextmanager
    def broken(name):
        with original(name) as db:
            class Fail:
                def __getattr__(self,name):return getattr(db,name)
                def commit(self):raise RuntimeError('disk-failure')
            yield Fail()
    monkeypatch.setattr(store,'db',broken)
    with pytest.raises(RuntimeError,match='disk-failure'):store.due_live_stream(time.time())
    assert 'valid' in store.live_stream_jobs
    monkeypatch.setattr(store,'db',original)
    assert store.due_live_stream(time.time())[0]['signature']=='valid'


@pytest.mark.asyncio
async def test_two_live_workers_do_not_wait_for_another_build_or_claim_twice(config,store,monkeypatch):
    service=Service(config,store)
    entered=[];both=asyncio.Event();release=asyncio.Event()
    async def process(row,rpc):
        entered.append(row['signature'])
        if len(entered)==2:both.set()
        await release.wait()
    monkeypatch.setattr(service,'process',process)
    store.enqueue(['older-history'],time.time()-10)
    for sig in ['first','second']:
        store.stream_transaction(live_payload(sig))
        store.stream_transaction(live_payload(sig))
    tasks=[asyncio.create_task(service.worker(None,True)) for _ in range(2)]
    try:
        await asyncio.wait_for(both.wait(),2)
        assert entered==['second','first']
        assert service.active_jobs=={'first','second'}
        assert store.rows('funding',"SELECT due,first_seen FROM jobs WHERE signature='older-history'")[0]['due']<time.time()
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    assert not service.active_jobs


def test_buffered_audit_keeps_original_times_dedupe_and_failed_batch(store,monkeypatch):
    writer=BufferedDecisions(store,Priority())
    store.decision_writer=writer
    monkeypatch.setattr(time,'time',lambda:1234.5)
    for _ in range(2):record(store,'execution','submitted','submitted',signature='tx',detail={'ms':12})
    assert not store.rows('audit','SELECT * FROM decisions')
    original=store.db
    @contextmanager
    def broken(name):
        with original(name) as db:
            class Fail:
                def __getattr__(self,name):return getattr(db,name)
                def commit(self):raise RuntimeError('disk-failure')
            yield Fail()
    monkeypatch.setattr(store,'db',broken)
    with pytest.raises(RuntimeError,match='disk-failure'):writer.flush()
    assert len(writer.pending)==2
    monkeypatch.setattr(store,'db',original)
    assert not store.rows('audit','SELECT * FROM decisions')
    monkeypatch.setattr(time,'time',lambda:9999)
    writer.close()
    rows=store.rows('audit','SELECT * FROM decisions')
    assert len(rows)==1 and rows[0]['time']==1234.5
    assert json.loads(rows[0]['detail'])=={'ms':12}


@pytest.mark.asyncio
async def test_service_shutdown_flushes_audit_and_closes_reused_connections(config,store,monkeypatch):
    service=Service(config,store)
    async def run():
        record(store,'execution','submitted','submitted',signature='shutdown')
    monkeypatch.setattr(service,'_run',run)
    await service.run()
    assert not store._idle_connections and store.decision_writer is None
    assert store.rows('audit',"SELECT signature FROM decisions")==[{'signature':'shutdown'}]


@pytest.mark.asyncio
async def test_build_carries_priority_once_and_signed_bytes_are_committed_before_send(config,store,monkeypatch):
    cfg,route,oid,result,_,_=await fixture(config,store)
    priority=Priority();builder=CachedBuilder(cfg,store,Notices(),priority)
    commands=[]
    class Input:
        def write(self,data):
            msg=json.loads(data);commands.append(msg['input'])
            builder.pending[msg['id']].set_result({'result':result})
        async def drain(self):pass
    async def start():builder.process=SimpleNamespace(stdin=Input())
    monkeypatch.setattr(builder,'start',start)
    class Public(RPC):
        async def call(self,method,params):
            assert method=='sendTransaction'
            assert [c['operation'] for c in commands]==['build']
            assert commands[0]['activeTrades']==1
            # Read from an independent connection, not the execution connection.
            saved=Store(config.data).order(oid)
            assert saved['state']=='signed' and saved['raw']==params[0]
            assert saved['signature'] and saved['min_out']==result['minOut']
            return await super().call(method,params)
    rpc=Public(store,oid)
    await Executor(cfg,store,rpc,Notices(),priority,builder).buy(oid,route)
    assert [c['operation'] for c in commands]==['build','priority']
    assert commands[-1]['activeTrades']==0 and commands[-1]['active']==0
    assert priority.active==0 and rpc.calls==['sendTransaction']
    detail=json.loads(store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]['detail'])
    assert detail['submit_ack_at']>=detail['submit_started_at']>0

