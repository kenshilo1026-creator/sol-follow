"""A ready funding backlog must not starve feeds, health, or shutdown."""
import asyncio
import time

import pytest

from features.runtime.service import Service


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['expired', 'immediate-result', 'immediate-error', 'retry-expired'])
async def test_worker_yields_while_backlog_remains(config, store, monkeypatch, path):
    signatures = [f'fairness-{i}' for i in range(16)]
    store.enqueue(signatures)
    if path == 'expired':
        with store.db('funding') as db:
            db.execute('UPDATE jobs SET first_seen=?', (time.time()-config.backfill_age-1,))
    service = Service(config, store)
    processed = []

    async def process(row, rpc):
        processed.append(row['signature'])
        if path in ('immediate-error', 'retry-expired'):
            raise ValueError('synthetic-worker-error')
        store.job_result(row['signature'], 'done')

    monkeypatch.setattr(service, 'process', process)
    if path == 'retry-expired':
        retry = store.retry
        monkeypatch.setattr(store, 'retry', lambda row, reason, now, max_age: retry(row, reason, now, 0))
    worker = asyncio.create_task(service.worker(None))
    try:
        # Give the worker a turn, then act as a ready feed/health task.
        # Without a cooperative yield, it drains the entire backlog first.
        for _ in range(3):
            await asyncio.sleep(0)
            progress = service.expired if path == 'expired' else len(processed)
            if progress:
                break
        assert 0 < progress < len(signatures)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert len(store.rows('funding', "SELECT * FROM jobs WHERE state='pending'")) > 0
        if path in ('expired', 'retry-expired'):
            kind, detail, alert = service.notices.queue.get_nowait()
            assert kind == ('qualification stopped' if path == 'retry-expired'
                            else 'qualification expired; possible coverage loss')
            assert detail['signature'] in signatures
            assert alert is False  # Retained for audit/logs, never sent to Telegram.
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
