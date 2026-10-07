"""Bounded source recovery cannot turn history into a live buy signal."""
import asyncio
from dataclasses import replace
import time

import pytest

from chain_common.transaction import Tx
from features.database.storage import Store
from features.funding.decoder import Funding
from features.funding.discovery import Discovery, history_page
from features.runtime.service import Service
from tests.helpers import Notices, address
from tests.test_native_buy import sample
from trade_execution.route import decode_all


@pytest.mark.asyncio
async def test_source_gap_has_three_page_budget_without_latest_repeats(store):
    now = int(time.time())
    calls = []
    class RPC:
        async def call(self, method, params):
            calls.append(params[1])
            return [dict(signature=f'{len(calls)}-{i}', err=None, blockTime=now) for i in range(100)]
    notices = Notices()
    for _ in range(3):
        await history_page(RPC(), store, 'source', 3600, notices)
    assert len(calls) == 3
    assert 'before' not in calls[0]
    assert calls[1]['before'] == '1-99' and calls[2]['before'] == '2-99'
    assert store.cursor('source')['gaps'] == []
    assert len(store.rows('funding', 'SELECT * FROM jobs')) == 300
    assert all(not store.is_live(row['signature'], 15) for row in store.rows('funding', 'SELECT signature FROM jobs'))
    assert any(args[0] == 'funding gap page limit reached' for args, _ in notices.items)


@pytest.mark.asyncio
async def test_short_gap_excludes_old_and_unknown_timestamps(store):
    now = int(time.time())
    class RPC:
        async def call(self, method, params):
            return [dict(signature=name, err=None, blockTime=stamp) for name, stamp in
                    [('recent', now-30), ('old', now-121), ('unknown', None)]]
    await history_page(RPC(), store, 'source', 3600, Notices())
    assert store.rows('funding', 'SELECT signature FROM jobs') == [{'signature': 'recent'}]


@pytest.mark.asyncio
async def test_new_hotlist_never_schedules_history_and_old_requests_drain(config, store):
    wallet = address()
    source = next(iter(config.cex))
    class RPC:
        async def call(self, *args):
            raise AssertionError('hotlist history must not call RPC')
    discovery = Discovery(config, store, RPC(), Notices())
    discovery.request_history(wallet)
    assert not store.rows('funding', 'SELECT * FROM history_requests')
    # Simulate a request left by the previous release.
    store.request_history(wallet)
    runner = asyncio.create_task(discovery.urgent_history())
    try:
        async with asyncio.timeout(2):
            while store.rows('funding', 'SELECT * FROM history_requests'):
                await asyncio.sleep(0)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    discovery.request_history(source)
    assert store.rows('funding', 'SELECT address FROM history_requests') == [{'address': source}]


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery', ['history', 'live', 'restart'])
async def test_only_live_receipt_can_vote_and_buy(config, store, delivery):
    raw = sample()
    raw['blockTime'] = int(time.time())
    routes = decode_all(Tx(raw))
    cfg = replace(config, n=1, hotlist_commitment='confirmed')
    for route in routes:
        store.admit(Funding('fund-'+route.trade.wallet, 'fund', route.trade.wallet, address(),
                    'cex:test', 10**9, route.trade.slot-1, raw['blockTime']-10), 3600, time.time())
    sig = raw['transaction']['signatures'][0]
    store.enqueue([sig], live=delivery != 'history')
    if delivery == 'restart':
        store = Store(cfg.data)
    service = Service(cfg, store)
    service.signals.started = raw['blockTime']-1
    calls = []
    class Engine:
        async def buy(self, oid, route):
            calls.append(oid)
    class RPC:
        async def transaction(self, signature):
            return raw
    service.executor = Engine()
    await service.process({'signature': sig}, RPC())
    assert bool(calls) == (delivery == 'live')
    assert bool(store.rows('trading', 'SELECT * FROM votes')) == (delivery == 'live')


def test_live_receipt_expires_and_duplicates_do_not_renew_it(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('features.database.storage.time.monotonic', lambda: clock[0])
    store.enqueue(['sig'], live=True)
    clock[0] += 16
    store.enqueue(['sig'], live=True)
    assert not store.is_live('sig', 15)


@pytest.mark.parametrize('commitment', ['processed', 'confirmed'])
def test_grpc_marks_only_transactions_above_live_slot_barrier(config, store, commitment):
    from features.funding.grpc_feed import HotlistFeed
    from tests.test_streaming import wire
    raw = sample()
    raw['blockTime'] = int(time.time())
    cfg = replace(config, hotlist_commitment=commitment)
    feed = HotlistFeed(cfg, store, None, Notices(), None)
    sig = raw['transaction']['signatures'][0]
    feed.anchor = (raw['slot'], raw['blockTime'], time.time())
    feed.live_floor = raw['slot']+1
    feed.accept(wire(raw))
    assert not store.is_live(sig, 15)
    # Use a distinct signature: replayed non-live processed proofs stay non-live.
    from chain_common.primitives import b58encode
    raw['transaction']['signatures'][0] = b58encode(bytes([42])*64)
    feed.live_floor = raw['slot']
    feed.accept(wire(raw))
    assert store.is_live(raw['transaction']['signatures'][0], 15)


@pytest.mark.asyncio
async def test_stale_untouched_queue_expires_without_rpc(config, store):
    store.enqueue(['stale'], time.time()-121)
    service = Service(config, store)
    class RPC:
        async def transaction(self, *args):
            raise AssertionError('stale recovery should expire before RPC')
    task = asyncio.create_task(service.worker(RPC()))
    try:
        async with asyncio.timeout(2):
            while store.rows('funding', 'SELECT state FROM jobs')[0]['state'] != 'expired':
                await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
