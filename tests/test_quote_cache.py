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
    assert len(builder.queue) == 1
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
        assert payload['risk'] == {'quoteTokenTaxBps': 390, 'tokenTaxBps': 390, 'impactBps': 200}
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


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['build','bootstrap'])
async def test_create_cache_does_not_enable_usd_oracles(config,store,operation):
    from dataclasses import replace
    builder=manager(replace(config,max_market_cap_usd_micros=1,max_observed_buy_usd_micros=999),store)
    async def ready():pass
    async def exchange(payload):
        assert payload['marketCapEnabled'] is False
        return {'checked':True}
    builder.start=ready;builder.exchange=exchange
    assert await builder.request({'route':'prime','minSlot':0,'lookupTables':[],'primeAccounts':[]},
                                 '',operation=operation)=={'checked':True}


@pytest.mark.asyncio
@pytest.mark.parametrize('stuck',['worker-start','worker-write','worker-response'])
async def test_buy_deadline_bounds_whole_worker_exchange(config,store,stuck):
    import time
    builder=manager(config,store)
    class Input:
        def write(self,data):pass
        async def drain(self):
            if stuck=='worker-write':await asyncio.Future()
    class Process:
        returncode=None
        stdin=Input()
        def kill(self):self.returncode=-1
    child=Process()
    async def start():
        if stuck=='worker-start':await asyncio.Future()
        builder.process=child
    builder.start=start
    began=time.monotonic()
    with pytest.raises(BuildError,match='signal-expired') as caught:
        await builder.exchange({'operation':'build','buySignalExpiresAtMs':int((time.time()+.08)*1000)})
    assert time.monotonic()-began<1
    assert caught.value.builder_details['phase']==stuck
    assert caught.value.builder_details['timeout_s']<=.08
    assert child.returncode==(-1 if stuck!='worker-start' else None)
    assert not builder.pending and not builder.progress


@pytest.mark.asyncio
async def test_expired_buy_never_starts_node(config,store):
    import time
    builder=manager(config,store)
    async def forbidden():raise AssertionError('expired buy started worker')
    builder.start=forbidden
    with pytest.raises(BuildError,match='signal-expired'):
        await builder.exchange({'operation':'build','buySignalExpiresAtMs':int((time.time()-1)*1000)})


@pytest.mark.asyncio
async def test_progress_does_not_complete_request_and_is_sanitized(config,store):
    builder=manager(config,store);stream=asyncio.StreamReader()
    class Process:
        stdout=stream;returncode=None
        def kill(self):self.returncode=1
        async def wait(self):return 1
    future=asyncio.get_running_loop().create_future();builder.pending[7]=future
    reader=asyncio.create_task(builder.read(Process()))
    def line(row):stream.feed_data((json.dumps(row)+'\n').encode())
    line({'id':7,'progress':{'stage':'simulateTransaction','phase':'start','ms':10,'url':'secret'}})
    line({'id':7,'progress':{'stage':'secret-endpoint','phase':'start','ms':10}})
    line({'id':999,'progress':{'stage':'build','phase':'start','ms':10}})
    await asyncio.sleep(0)
    assert not future.done()
    assert builder.progress=={7:[{'stage':'simulateTransaction','phase':'start','ms':10}]}
    line({'id':7,'result':{'warmed':True}})
    assert (await asyncio.wait_for(future,1))['result']=={'warmed':True}
    stream.feed_eof();await reader


@pytest.mark.asyncio
async def test_worker_build_error_retains_last_progress(config,store):
    builder=manager(config,store)
    class Input:
        def write(self,data):
            row=json.loads(data);identifier=row['id']
            builder.progress[identifier]=[{'stage':'getMultipleAccountsInfo','phase':'start','ms':2}]
            builder.pending[identifier].set_result({'error':'execution-context-behind'})
        async def drain(self):pass
    async def start():builder.process=SimpleNamespace(stdin=Input())
    builder.start=start
    with pytest.raises(BuildError,match='execution-context-behind') as caught:
        await builder.exchange({'operation':'build'})
    assert caught.value.builder_details['progress'][0]['stage']=='getMultipleAccountsInfo'
    assert not builder.pending and not builder.progress


@pytest.mark.asyncio
async def test_builder_diagnostics_reach_failed_order_audit(config,store):
    from tests.test_pump_buy import setup_order,RPC
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    cfg,r,oid,_=setup_order(config,store)
    detail={'phase':'worker-response','progress':[{'stage':'simulateTransaction','phase':'start','ms':12}]}
    async def build(*args):
        error=BuildError('signal-expired');error.builder_details=detail;raise error
    await Executor(cfg,store,RPC(),Notices(),Priority(),build).buy(oid,r)
    assert store.order(oid)['state']=='failed' and store.order(oid)['signature'] is None
    audit=store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]
    assert json.loads(audit['detail'])['builder_diagnostics']==detail


@pytest.mark.asyncio
async def test_worker_ready_is_bound_to_current_process_and_does_not_complete_request(config,store):
    builder=manager(config,store)
    stream=asyncio.StreamReader()
    class Process:
        stdout=stream;returncode=None
        def kill(self):self.returncode=1
        async def wait(self):return 1
    process=Process();builder.process=process
    future=asyncio.get_running_loop().create_future();builder.pending[8]=future
    reader=asyncio.create_task(builder.read(process))
    def line(row):stream.feed_data((json.dumps(row)+'\n').encode())
    line({'worker':'ready','boot_ms':float('nan')})
    await asyncio.sleep(0)
    assert builder.worker_ready is None
    line({'worker':'ready','boot_ms':123.4567,'url':'secret'})
    await asyncio.sleep(0)
    assert builder.worker_ready is process and builder.worker_boot_ms==123.457
    assert not future.done()
    replacement=SimpleNamespace();builder.process=replacement;builder.worker_ready=None
    line({'worker':'ready','boot_ms':999})
    await asyncio.sleep(0)
    assert builder.worker_ready is None
    line({'id':8,'result':{}})
    await future
    stream.feed_eof();await reader


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement_already_started',[False,True])
async def test_idle_warmer_reinitializes_replacement_and_yields_to_trade(config,store,replacement_already_started):
    builder=manager(config,store);calls=[]
    first=asyncio.Event();recovered=asyncio.Event()
    async def start():
        if builder.process is None or builder.process.returncode is not None:
            builder.process=SimpleNamespace(returncode=None)
    async def request(request,wallet,**kwargs):
        assert kwargs=={'operation':'bootstrap'}
        calls.append(builder.process)
        (first if len(calls)==1 else recovered).set()
        return {'warmed':True}
    builder.start=start;builder.request=request
    task=asyncio.create_task(builder.warm())
    try:
        await asyncio.wait_for(first.wait(),1)
        builder.priority.active=1
        old=builder.process
        if replacement_already_started:
            builder.process=SimpleNamespace(returncode=None)
        else:old.returncode=-1
        await asyncio.sleep(.25)
        assert len(calls)==1 and not builder.queue
        builder.priority.active=0
        await asyncio.wait_for(recovered.wait(),1)
        assert len(calls)==2 and calls[-1] is builder.process and calls[-1] is not old
        assert builder.warmed_process is builder.process
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


def test_warm_queue_is_bounded_by_quote_and_restored_after_worker_replacement(config,store):
    builder=manager(config,store)
    original=route()
    for _ in range(140):
        r=replace(original,quote_mint=address(),trade=replace(original.trade,mint=address()))
        builder.observe(r)
    assert len(builder.queue)==128
    builder.queue.clear();builder.shared_seen.add('old-process-only')
    builder.restore_warm_queue()
    assert len(builder.queue)==128 and not builder.shared_seen
    assert all(req['mint']==req['quoteMint'] for req,wallet in builder.queue.values())
    assert all(req['minSlot']==0 and wallet for req,wallet in builder.queue.values())


@pytest.mark.asyncio
async def test_background_warms_quote_independently_of_old_target_and_yields(config,store):
    builder=manager(config,store);builder.observe(route());calls=[];done=asyncio.Event()
    async def start():
        if builder.process is None:builder.process=SimpleNamespace(returncode=None)
    async def request(request,wallet,**kwargs):
        calls.append((request,kwargs))
        if kwargs.get('operation')=='quote_warm':
            assert not builder.priority.active
            assert kwargs.get('warm') is True
            assert request['mint']==request['quoteMint'] and request['minSlot']==0
            done.set()
        return {'warmed':True}
    builder.start=start;builder.request=request;builder.priority.active=1
    task=asyncio.create_task(builder.warm())
    try:
        await asyncio.sleep(.05)
        assert not done.is_set()
        builder.priority.active=0
        await asyncio.wait_for(done.wait(),1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


@pytest.mark.asyncio
async def test_persisted_quote_route_survives_database_reopen_without_old_target(config):
    first=Store(config.data)
    r=route()
    fixture=json.loads((FIX/'local_dlmm_quote_accounts.json').read_text())
    recipe=fixture['recipe']
    request={**r.request(),'quoteMint':recipe['steps'][-1]['outputMint']}
    seen=SeenTokens(first)
    seen.remember(request,r.trade.wallet)
    seen.save_recipe(request['quoteMint'],recipe)
    # Route identity must survive independently of old target/history records.
    with first.db('funding') as db:
        db.execute('DELETE FROM seen_non_sol')
    first.close()

    reopened=Store(config.data)
    try:
        builder=manager(config,reopened)
        builder.restore_warm_queue()
        restored,wallet=builder.queue[request['quoteMint']]
        assert restored['mint']==restored['quoteMint']==request['quoteMint']
        assert restored['quoteProgram']==request['quoteProgram']
        assert restored['minSlot']==0 and 'pool' not in restored
        captured=[]
        async def exchange(payload):
            captured.append(payload)
            return {'warmed':True}
        builder.exchange=exchange
        await builder.request(restored,r.trade.wallet,warm=True,operation='quote_warm')
        assert captured[0]['swapRecipe']==recipe
        assert captured[0]['swapRecipe']['tables']
        assert captured[0]['operation']=='quote_warm'
        assert captured[0]['minSlot']==0
        assert captured[0]['amount']==str(config.buy_amount)
        assert 'quoted_out' not in captured[0] and 'signedTransaction' not in captured[0]
    finally:
        reopened.close()
