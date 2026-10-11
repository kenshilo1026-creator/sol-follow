"""Restart cleanup and process-local live priority for admission work."""
import asyncio
import time

import pytest

from features.database.storage import Store
from features.runtime.service import Service
from tests.test_funding_rpc_recovery import config, store


def payload(store, signature):
    with store.db('funding') as db:
        db.execute('INSERT INTO stream_payloads VALUES (?,?)', (signature, '{}'))


def test_startup_removes_only_pending_jobs_older_than_one_hour(store):
    now = 10000
    for signature, age in [('stale', 3601), ('retry', 72703), ('boundary', 3600),
                           ('recent', 3599), ('done', 4000), ('expired', 4000)]:
        store.enqueue([signature], now-age)
    store.job_result('done', 'done')
    store.job_result('expired', 'expired')
    for signature in ('stale', 'retry', 'boundary', 'recent'):
        payload(store, signature)
    with store.db('funding') as db:
        db.execute("UPDATE jobs SET due=?,reason='RpcError',attempts=10 WHERE signature='retry'", (now+60,))
        db.execute("INSERT INTO chain_checks VALUES ('stale',100,1)")
        db.execute("INSERT INTO processed_signals VALUES ('stale',100,1,1,'pending',1,'')")
    with store.db('trading') as db:
        db.execute("INSERT INTO orders(id,state,signature) VALUES ('order','submitted','own-tx')")
        db.execute("INSERT INTO order_sources VALUES ('order','stale',100)")
        db.execute("INSERT INTO positions(mint,mode,amount) VALUES ('mint','live','10')")
    preserved = {table: store.rows(database, f'SELECT * FROM {table}')
                 for database, table in [('funding', 'chain_checks'), ('funding', 'processed_signals'),
                                         ('trading', 'orders'), ('trading', 'order_sources'), ('trading', 'positions')]}

    assert store.prune_startup_jobs(now) == 2
    assert {r['signature'] for r in store.rows('funding', 'SELECT * FROM jobs')} == {
        'boundary', 'recent', 'done', 'expired'}
    assert {r['signature'] for r in store.rows('funding', 'SELECT * FROM stream_payloads')} == {'boundary', 'recent'}
    for table, rows in preserved.items():
        database = 'funding' if table in ('chain_checks', 'processed_signals') else 'trading'
        assert store.rows(database, f'SELECT * FROM {table}') == rows
    assert store.prune_startup_jobs(now) == 0


@pytest.mark.asyncio
async def test_every_start_prunes_before_network_or_workers(config, store, monkeypatch, caplog):
    def stop_before_network(**kwargs):
        assert not store.rows('funding', "SELECT * FROM jobs WHERE signature LIKE 'stale-%'")
        raise RuntimeError('startup-checked')

    monkeypatch.setattr('features.runtime.service.aiohttp.ClientSession', stop_before_network)
    caplog.set_level('INFO')
    for attempt in range(2):
        store.enqueue([f'stale-{attempt}'], time.time()-3601)
        restarted = Store(config.data)
        service = Service(config, restarted)
        with pytest.raises(RuntimeError, match='startup-checked'):
            await service._run()
        assert service.backlog.previous == 0
    assert caplog.text.count('startup queue cleanup removed=1 max_age_s=3600') == 2


@pytest.mark.parametrize('oldest', [False, True])
@pytest.mark.parametrize('streamed', [None, False, True])
def test_live_receipts_always_precede_history_in_each_lane(store, monkeypatch, oldest, streamed):
    store.enqueue(['old-history'], 800)
    store.enqueue(['new-history'], 999)
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 950)
    store.enqueue(['older-live'], 900, live=True)
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 960)
    store.enqueue(['newer-live'], 901, live=True)
    if streamed is not None:
        if streamed:
            for signature in ('old-history', 'new-history', 'older-live', 'newer-live'):
                payload(store, signature)
        store.enqueue(['other-lane'], 999, live=True)
        if not streamed:
            payload(store, 'other-lane')

    rows = store.due(4, 1000, oldest=oldest, streamed=streamed)
    history = ['old-history', 'new-history'] if oldest else ['new-history', 'old-history']
    assert [r['signature'] for r in rows] == ['newer-live', 'older-live', *history]
    # Priority must still respect the claim lease.
    assert store.due(4, 1000, streamed=streamed) == []


def test_live_backoff_is_respected_and_expired_receipts_lose_priority(store, monkeypatch):
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 600)
    store.enqueue(['stale-live'], 800, live=True)
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 1000)
    store.enqueue(['waiting-live'], 900, live=True)
    store.enqueue(['history'], 999)
    with store.db('funding') as db:
        db.execute("UPDATE jobs SET due=1001 WHERE signature='waiting-live'")
    assert [r['signature'] for r in store.due(3, 1000)] == ['history', 'stale-live']
    assert [r['signature'] for r in store.due(3, 1001)] == ['waiting-live']


def test_restart_does_not_restore_live_priority(store, monkeypatch):
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 1000)
    store.enqueue(['previous-live'], 800, live=True)
    store.enqueue(['history'], 999)
    payload(store, 'previous-live')
    restarted = Store(store.directory)
    assert [r['signature'] for r in restarted.due(2, 1000)] == ['history', 'previous-live']


def test_live_priority_expires_without_another_feed_event(store, monkeypatch):
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 600)
    store.enqueue(['old-live'], 800, live=True)
    store.enqueue(['new-history'], 999)
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: 901)
    assert [r['signature'] for r in store.due(2, 1000)] == ['new-history', 'old-live']


@pytest.mark.asyncio
async def test_worker_prioritizes_live_even_on_fifth_oldest_turn(config, store, monkeypatch):
    store.enqueue(['history'], time.time()-30)
    service = Service(config, store)
    seen = []

    async def process(row, rpc):
        seen.append(row['signature'])
        store.job_result(row['signature'], 'done')
        if len(seen) < 5:
            store.enqueue([f'live-{len(seen)}'], live=True)

    # Stop deterministically before the sixth claim, outside worker's process catch.
    due = store.due

    def claim(*args, **kwargs):
        if len(seen) == 5:
            raise asyncio.CancelledError
        return due(*args, **kwargs)

    monkeypatch.setattr(service, 'process', process)
    monkeypatch.setattr(store, 'due', claim)
    store.enqueue(['live-0'], live=True)
    with pytest.raises(asyncio.CancelledError):
        await service.worker(None)
    assert seen == [f'live-{i}' for i in range(5)]
    assert store.rows('funding', "SELECT state FROM jobs WHERE signature='history'") == [{'state': 'pending'}]
