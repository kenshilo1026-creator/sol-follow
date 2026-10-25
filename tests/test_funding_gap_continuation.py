"""Source catch-up budgets must yield without discarding unread signatures."""
import sqlite3

import pytest

from features.database.storage import Store
from features.funding.discovery import Discovery, history_page
from tests.helpers import Notices


class History:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def call(self, method, params):
        assert method == 'getSignaturesForAddress'
        address, options = params
        self.calls.append((address, dict(options)))
        rows = self.rows
        signatures = [r['signature'] for r in rows]
        start = signatures.index(options['before'])+1 if options.get('before') else 0
        end = signatures.index(options['until']) if options.get('until') in signatures else len(rows)
        return rows[start:end][:options['limit']]


def recent(count, stamp=990):
    return [dict(signature=f's{i}', err=None, blockTime=stamp) for i in range(count)]


@pytest.mark.asyncio
async def test_450_signatures_resume_after_budget_restart_and_window_age(config, store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('features.funding.discovery.time.time', lambda: clock[0])
    source = next(iter(config.cex))
    store.save_cursor(source, {'latest': 'previous'}, [])
    rpc = History(recent(450))
    discovery = Discovery(config, store, rpc, Notices())
    assert await discovery.page(source) is True
    assert len(rpc.calls) == 3
    state = store.cursor(source)
    assert state['latest'] == 'previous'
    assert state['observed_latest'] == 's0'
    assert state['gaps'][0]['before'] == 's299'
    assert state['gaps'][0]['cutoff'] == 880
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 300

    # Passing the former 120s/240s expiry cannot shrink or discard this gap.
    clock[0] += 300
    restarted = Store(config.data)
    discovery = Discovery(config, restarted, rpc, Notices())
    assert await discovery.page(source) is False
    assert len(rpc.calls) == 5
    assert rpc.calls[3][1]['before'] == 's299'
    assert rpc.calls[4][1]['before'] == 's399'
    assert all(options['until'] == 'previous' for _, options in rpc.calls)
    assert restarted.cursor(source)['latest'] == 's0'
    assert restarted.cursor(source)['gaps'] == []
    jobs = restarted.rows('funding', 'SELECT * FROM jobs')
    assert len(jobs) == 450
    assert all(not restarted.is_live(r['signature'], 15) for r in jobs)

    rpc.rows.insert(0, dict(signature='new', err=None, blockTime=1300))
    assert await discovery.page(source) is False
    assert rpc.calls[-1][1]['until'] == 's0'
    assert 'before' not in rpc.calls[-1][1]
    assert restarted.cursor(source)['latest'] == 'new'
    assert len(restarted.rows('funding', 'SELECT * FROM jobs')) == 451


@pytest.mark.asyncio
async def test_legacy_gap_keeps_original_anchor_even_after_three_pages(store, monkeypatch):
    monkeypatch.setattr('features.funding.discovery.time.time', lambda: 1400)
    store.save_cursor('source', {'latest':'s0', 'gaps':[
        dict(before='s299', until='previous', cutoff=880, pages=3)]}, [])
    rpc = History(recent(350))
    assert await history_page(rpc, store, 'source', 3600, Notices()) is False
    assert rpc.calls[0][1]['before'] == 's299'
    assert rpc.calls[0][1]['until'] == 'previous'
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 50
    assert store.cursor('source')['latest'] == 's0'


@pytest.mark.asyncio
async def test_page_jobs_and_cursor_rollback_together_on_db_error(store, monkeypatch):
    monkeypatch.setattr('features.funding.discovery.time.time', lambda: 1000)
    rpc = History(recent(150))
    await history_page(rpc, store, 'source', 3600, Notices())
    checkpoint = store.cursor('source')
    with store.db('funding') as db:
        db.execute("CREATE TRIGGER reject_cursor BEFORE UPDATE ON cursors BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await history_page(rpc, store, 'source', 3600, Notices())
    assert store.cursor('source') == checkpoint
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 100
    with store.db('funding') as db:
        db.execute('DROP TRIGGER reject_cursor')
    assert await history_page(rpc, store, 'source', 3600, Notices()) is False
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['rpc', 'repeated', 'malformed'])
async def test_failed_page_does_not_advance_or_lose_checkpoint(store, monkeypatch, failure):
    monkeypatch.setattr('features.funding.discovery.time.time', lambda: 1000)
    await history_page(History(recent(150)), store, 'source', 3600, Notices())
    checkpoint = store.cursor('source')
    class Broken:
        async def call(self, *args):
            if failure == 'rpc':
                raise RuntimeError('429')
            if failure == 'repeated':
                return recent(100)
            return [{'err': None}]
    with pytest.raises((RuntimeError, ValueError)):
        await history_page(Broken(), store, 'source', 3600, Notices())
    assert store.cursor('source') == checkpoint
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 100


def test_continuation_yields_fairly_and_resets_only_failure_backoff(store, monkeypatch):
    monkeypatch.setattr('features.database.storage.time.time', lambda: 1000)
    store.request_histories(['busy', 'other'])
    row = store.rows('funding', "SELECT * FROM history_requests WHERE address='busy'")[0]
    store.finish_history(row, retry=True)
    failed = store.rows('funding', "SELECT * FROM history_requests WHERE address='busy'")[0]
    assert failed['attempts'] == 1
    store.finish_history(failed, more=True)
    rows = store.rows('funding', 'SELECT * FROM history_requests ORDER BY due')
    assert [r['address'] for r in rows] == ['other', 'busy']
    assert rows[1]['attempts'] == 0 and rows[1]['due'] == 1001
    # A reconnect during a scan must survive both completion and continuation.
    store.request_history('busy')
    store.finish_history(rows[1], more=True)
    store.finish_history(rows[1])
    renewed = store.rows('funding', "SELECT * FROM history_requests WHERE address='busy'")[0]
    assert renewed['revision'] == rows[1]['revision']+1 and renewed['due'] == 1000


@pytest.mark.asyncio
async def test_fixed_cutoff_stops_at_old_page_and_unknown_time_does_not_stop(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('features.funding.discovery.time.time', lambda: clock[0])
    rows = recent(100)
    rows[0]['blockTime'] = None
    rows += [dict(signature='eligible', err=None, blockTime=885),
             dict(signature='old', err=None, blockTime=879)]
    rpc = History(rows)
    assert await history_page(rpc, store, 'source', 3600, Notices()) is True
    clock[0] = 1100
    assert await history_page(rpc, store, 'source', 3600, Notices()) is False
    signatures = {r['signature'] for r in store.rows('funding', 'SELECT signature FROM jobs')}
    assert 'eligible' in signatures and 'old' not in signatures and 's0' not in signatures
