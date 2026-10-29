"""Expired maintenance deadlines must never leak to a pooled connection borrower."""
import sqlite3
import time

import pytest

from chain_common.rpc import Priority
from features.database.errors import detail, failures
from features.database.maintenance import Maintenance, batch
from share_common.notify import message
from tests.helpers import Notices


def test_cleanup_deadline_does_not_interrupt_next_queue_claim(store,monkeypatch):
    store.enqueue([f'job-{i}' for i in range(2000)],now=1)
    clock=[100.0]
    monkeypatch.setattr('features.database.maintenance.time.monotonic',lambda:clock[0])
    assert batch(store,'funding','jobs','first_seen<?',(-1,))==0
    clock[0]+=1  # The previous cleanup's 40ms budget has expired.
    assert len(store.due(1,1000))==1
    assert len(store.rows('funding','SELECT * FROM jobs'))==2000


def test_activity_retention_does_not_scan_unexpired_history(store,monkeypatch):
    with store.db('funding') as db:
        db.execute('BEGIN')
        db.executemany('INSERT INTO funding_activity_checks VALUES (?,?,?,?)',
                       [(f'active-{i}',1,10000,'{}') for i in range(25000)])
        db.executemany('INSERT INTO funding_activity_checks VALUES (?,?,?,?)',
                       [(f'expired-{i}',1,10,'{}') for i in range(10)])
        db.commit()
    # Deterministic VM-work budget: a full scan burns > 4 progress callbacks,
    # whereas a small indexed expiry batch fits. No machine-speed assertion.
    clock=[0.]
    def tick():
        clock[0]+=.01
        return clock[0]
    monkeypatch.setattr('features.database.maintenance.time.monotonic',tick)
    assert batch(store,'funding','funding_activity_checks','expires<?',(100,),limit=6)==6
    assert batch(store,'funding','funding_activity_checks','expires<?',(100,),limit=6)==4
    assert batch(store,'funding','funding_activity_checks','expires<?',(100,),limit=6)==0
    assert store.rows('funding','SELECT count(*) n FROM funding_activity_checks')[0]['n']==25000


@pytest.mark.asyncio
async def test_cleanup_deferred_logs_sqlite_code(config,store,monkeypatch):
    import features.database.maintenance as module
    with sqlite3.connect(':memory:') as db:
        db.set_progress_handler(lambda:1,1)
        with pytest.raises(sqlite3.OperationalError) as raised:db.execute('SELECT 1')
    def interrupted(*args):raise raised.value
    monkeypatch.setattr(module,'batch',interrupted)
    notices=Notices()
    await Maintenance(config,store,Priority(),notices).pass_once()
    detail=next(args[1] for args,kwargs in notices.items if args[0]=='cleanup deferred')
    assert detail['sqlite_name']=='SQLITE_INTERRUPT'


@pytest.mark.asyncio
async def test_full_maintenance_checkpoints_do_not_poison_any_database(config,store,monkeypatch):
    now=time.time()
    store.enqueue([f'job-{i}' for i in range(2000)],now=now)
    with store.db('audit') as db:
        db.executemany('INSERT INTO audit(time,kind,detail) VALUES (?,?,?)',[(now,'test','{}')]*2000)
    with store.db('trading') as db:
        db.executemany('INSERT INTO events VALUES (?,?,?)',[(str(i),now,str(i)) for i in range(2000)])
    clock=[100.0]
    monkeypatch.setattr('features.database.maintenance.time.monotonic',lambda:clock[0])
    notices=Notices()
    await Maintenance(config,store,Priority(),notices).pass_once()
    clock[0]+=1
    for name,table in [('audit','audit'),('funding','jobs'),('trading','events')]:
        assert len(store.rows(name,f'SELECT * FROM {table}'))==2000
    assert len(store.due(1,now+1))==1


def test_progress_handler_cleared_before_rollback_even_on_exception(store):
    with pytest.raises(RuntimeError,match='body-failure'):
        with store.db('trading') as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO events VALUES ('rollback',1,'sig')")
            db.set_progress_handler(lambda:1,1)
            raise RuntimeError('body-failure')
    assert not store.rows('trading','SELECT * FROM events')
    with store.db('trading') as db:
        db.execute("INSERT INTO events VALUES ('next-write',1,'sig')")


def test_interrupted_sqlite_error_and_exception_group_have_safe_details():
    with sqlite3.connect(':memory:') as db:
        db.set_progress_handler(lambda:1,1)
        with pytest.raises(sqlite3.OperationalError) as raised:
            db.execute('SELECT 1')
        db.set_progress_handler(None,0)
    info=detail(raised.value)
    assert info['sqlite_code']==sqlite3.SQLITE_INTERRUPT
    assert info['sqlite_name']=='SQLITE_INTERRUPT'
    assert '中斷' in info['reason']
    assert info['location'].startswith('test_sqlite_progress_reset.py:')
    error=sqlite3.OperationalError('https://rpc.example/private-key fixture-secret')
    combined=failures(ExceptionGroup('fixture-secret',[raised.value,ExceptionGroup('secret',[error])]))
    assert len(combined)==2 and 'fixture-secret' not in str(combined)
    notification=message('funding queue unavailable',info)
    assert 'SQLITE_INTERRUPT' in notification and '查詢被中斷' in notification


def test_positions_retained_message_is_recovery_not_new_buy():
    text=message('positions retained for exit management',{'orders':[],'position_mints':['mint']})
    assert '本地持倉紀錄: mint' in text and '0 筆' in text and '不是新增買單' in text
