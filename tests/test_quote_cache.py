import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.database.quote_cache import SeenTokens
from features.database.storage import Store
from features.runtime.service import Service
from launchpads.create import decode
from share_common.config import risk_bps
from trade_execution.cache import CachedBuilder
from trade_execution.builder import BuildError
from trade_execution.stonk import decode_stonk
from tests.helpers import Notices, address

FIX = Path(__file__).parent/'fixtures'


def route():
    return decode_stonk(Tx(json.loads((FIX/'stonk_non_sol_buy.json').read_text())))[0]


def manager(config, store):
    return CachedBuilder(config, store, Notices(), SimpleNamespace(active=0))


def test_every_seen_mint_is_durable_despite_bounded_active_cache(config, store):
    builder = manager(config, store)
    original = route()
    for _ in range(140):
        r = replace(original, trade=replace(original.trade, mint=address()))
        builder.observe(r)
    assert len(builder.queue) == 128
    assert len(store.rows('funding', 'SELECT * FROM seen_non_sol')) == 140
    assert len(store.rows('funding', 'SELECT * FROM seen_quote_mints')) == 1
    restarted = manager(config, Store(config.data))
    assert len(restarted.seen.recent(1000)) == 140
    recipe = {'version': 1, 'steps': [], 'tables': []}
    builder.seen.save_recipe(original.quote_mint, recipe)
    assert restarted.seen.recipe(original.quote_mint) == recipe


def test_non_sol_creates_cached_and_native_creates_excluded(config, store):
    builder = manager(config, store)
    for path in FIX.glob('create_*.json'):
        if path.name in ('create_samples.json', 'create_abi_sources.json'):
            continue
        for item in decode(Tx(json.loads(path.read_text()))):
            builder.observe_create(item)
    rows = builder.seen.recent()
    assert len(rows) == 2
    assert {r['route'] for r, _ in rows} == {'sol_to_pump_curve', 'sol_to_stonk_curve'}
    assert all(r['quoteMint'] != WSOL for r, _ in rows)


def test_create_or_delayed_event_cannot_downgrade_proven_route(store):
    seen = SeenTokens(store)
    req = route().request()
    seen.remember(req, address())
    prime = {**req, 'route': 'prime', 'minSlot': req['minSlot']+10}
    saved = seen.remember(prime, address())
    assert saved['route'] == req['route'] and saved['minSlot'] == prime['minSlot']
    assert seen.remember(req, address()) == saved


def test_startup_imports_existing_creates_without_unbounded_warm_queue(config, store):
    items = decode(Tx(json.loads((FIX/'create_4Nr7xPstQ6VT9MF1kKwmkRy3b1mXct9UPsNgs53Cpump.json').read_text())))
    store.record_creates(items)
    builder = manager(config, store)
    asyncio.run(builder.import_creates())
    assert builder.seen.recent()[0][0]['mint'] == items[0].mint
    assert not builder.queue


def test_service_records_buy_route_before_hotlist_qualification(config, store, monkeypatch):
    raw = json.loads((FIX/'stonk_non_sol_buy.json').read_text())
    service = Service(config, store)
    service.notices = Notices()
    monkeypatch.setattr('features.runtime.service.time.time', lambda: raw['blockTime']+1)
    raw_rpc = SimpleNamespace(transaction=lambda signature: asyncio.sleep(0, result=raw))
    signature = raw['transaction']['signatures'][0]
    store.mark_live(signature)
    asyncio.run(service.process({'signature': signature}, raw_rpc))
    assert len(service.quote_builder.seen.recent()) == 1
    assert store.rows('trading', 'SELECT * FROM orders') == []


@pytest.mark.parametrize('value', ['nan', 'Infinity', '-0.01', '20.01', '0.001'])
def test_risk_configuration_rejects_invalid_or_truncated_limits(value):
    with pytest.raises(ValueError):
        risk_bps(value)


def test_zero_fee_cap_and_decimal_percent_have_exact_units():
    assert risk_bps('0') == 0
    assert risk_bps('1.25') == 125


def test_worker_payload_restores_recipe_and_preserves_slot_amount_risk(config, store, monkeypatch):
    async def scenario():
        builder = manager(config, store)
        r = route()
        recipe = {'version': 1, 'steps': [], 'tables': []}
        builder.seen.save_recipe(r.quote_mint, recipe)
        captured = []
        class Input:
            def write(self, data):
                row = json.loads(data)
                captured.append(row['input'])
                builder.pending[row['id']].set_result({'result': {'warmed': True}})
            async def drain(self):
                pass
        async def start():
            builder.process = SimpleNamespace(stdin=Input())
        monkeypatch.setattr(builder, 'start', start)
        await builder.request(r.request(), r.trade.wallet, warm=True)
        payload = captured[0]
        assert payload['swapRecipe'] == recipe
        assert payload['minSlot'] == r.trade.slot
        assert payload['amount'] == str(config.buy_amount)
        assert payload['prewarm'] is True
        assert payload['risk'] == {'poolFeeBps': 200, 'totalFeeBps': 300, 'impactBps': 200}
        assert payload['rpc'] == config.rpc
        assert builder.pending == {}
    asyncio.run(scenario())


def test_reader_persists_discovery_and_fails_pending_build_on_child_exit(config, store):
    async def scenario():
        builder = manager(config, store)
        stream = asyncio.StreamReader()
        recipe = {'version': 1, 'steps': [], 'tables': []}
        stream.feed_data((json.dumps({'id': 1, 'recipe': recipe, 'quoteMint': route().quote_mint})+'\n').encode())
        stream.feed_eof()
        class Process:
            stdout = stream
            returncode = 1
            async def wait(self):
                return 1
        future = asyncio.get_running_loop().create_future()
        builder.pending[1] = future
        await builder.read(Process())
        with pytest.raises(BuildError, match='route-builder-stopped'):
            await future
        assert builder.seen.recipe(route().quote_mint) == recipe
    asyncio.run(scenario())
