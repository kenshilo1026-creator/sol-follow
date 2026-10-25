import asyncio
import base64
from copy import deepcopy
from dataclasses import replace

import pytest
from solders.keypair import Keypair
from solders.hash import Hash
from solders.transaction import VersionedTransaction

from chain_common.rpc import RpcError
from tests.helpers import Notices
from tests.test_pump_buy import setup_order
from trade_execution.executor import Executor
from trade_execution.rpc import ExecutionRpc, MAINNET
from chain_common.rpc import Priority
from share_common.config import load
from trade_execution.builder import payload_for


class Client:
    def __init__(self, result=None, *, genesis=MAINNET):
        self.result, self.genesis = result, genesis
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == 'getGenesisHash':
            return self.genesis
        value = self.result(method, params) if callable(self.result) else self.result
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)

    def snapshot(self):
        return {'calls': len(self.calls), 'rate_limited': 0}


def test_dedicated_rpc_configuration_reaches_sdk_without_exposing_urls_in_repr(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    cfg=load(tmp_path,env={'SOL_EXECUTION_RPC_HTTP_URL':'https://primary.example/private-token',
        'SOL_EXECUTION_RPC_FALLBACK_HTTP_URL':'https://backup.example/backup-token',
        'SOL_EXECUTION_RPC_WS_URL':'wss://primary.example/private-token',
        'SOL_EXECUTION_RPC_TIMEOUT_MS':'1500'})
    payload=payload_for(cfg,{},'wallet')
    assert payload['rpc']==cfg.execution_rpc_url
    assert payload['rpcFallback']==cfg.execution_fallback_url
    assert payload['ws']==cfg.execution_ws_url
    assert payload['rpcTimeoutMs']==1500
    assert 'private-token' not in repr(cfg) and 'backup-token' not in repr(cfg)


@pytest.mark.parametrize('name',['SOL_EXECUTION_RPC_HTTP_URL','SOL_EXECUTION_RPC_FALLBACK_HTTP_URL','SOL_EXECUTION_RPC_WS_URL'])
def test_invalid_execution_endpoint_fails_without_leaking_secret(tmp_path,name):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    with pytest.raises(ValueError,match='^invalid-execution-rpc-url$'):
        load(tmp_path,env={name:'file:///private-token'})


async def signed_request(config, store):
    key = Keypair()
    cfg, route, oid, builder = setup_order(config, store, key)
    result = await builder()
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(result['transaction']))
    signed = VersionedTransaction(unsigned.message, [key])
    raw = base64.b64encode(bytes(signed)).decode()
    return raw, str(signed.signatures[0]), cfg, route, oid, builder, key


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RpcError('HTTP-429'), asyncio.TimeoutError(), 'wrong-signature'])
async def test_send_fallback_preserves_identical_signed_bytes_and_preflight(config, store, failure):
    raw, signature, *_ = await signed_request(config, store)
    primary, fallback = Client(failure), Client(signature)
    router = ExecutionRpc(primary, fallback)
    params = [raw, {'encoding':'base64', 'skipPreflight':False, 'preflightCommitment':'processed',
                    'maxRetries':0, 'minContextSlot':123}]
    original = deepcopy(params)
    assert await router.call('sendTransaction', params) == signature
    assert primary.calls[-1] == fallback.calls[-1] == ('sendTransaction', original)
    assert params == original
    assert router.recovered == 1


@pytest.mark.asyncio
async def test_wrong_chain_never_receives_signed_transaction(config, store):
    raw, signature, *_ = await signed_request(config, store)
    primary, fallback = Client(signature, genesis=str(Hash.new_unique())), Client(signature)
    router = ExecutionRpc(primary, fallback)
    for _ in range(2):
        assert await router.call('sendTransaction', [raw, {'encoding':'base64'}]) == signature
    assert primary.calls == [('getGenesisHash', [])]


@pytest.mark.asyncio
async def test_missing_genesis_is_retryable_not_a_permanent_wrong_chain():
    primary=Client(100,genesis=None)
    rpc=ExecutionRpc(primary)
    with pytest.raises(RpcError):await rpc.call('getSlot',[])
    primary.genesis=MAINNET
    assert await rpc.call('getSlot',[])==100


@pytest.mark.asyncio
async def test_simulation_rejection_does_not_try_more_permissive_provider():
    rejected = {'context':{'slot':100}, 'value':{'err':{'InstructionError':[0,'Custom']}}}
    primary, fallback = Client(rejected), Client({'context':{'slot':100}, 'value':{'err':None}})
    assert await ExecutionRpc(primary, fallback).call('simulateTransaction', ['unsigned', {'minContextSlot':100}]) == rejected
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_stale_simulation_uses_fresh_backup():
    old = {'context':{'slot':99}, 'value':{'err':None}}
    fresh = {'context':{'slot':100}, 'value':{'err':None}}
    assert await ExecutionRpc(Client(old), Client(fresh)).call('simulateTransaction', ['unsigned', {'minContextSlot':100}]) == fresh


@pytest.mark.asyncio
async def test_status_null_uses_backup_without_erasing_primary_evidence():
    final = {'slot':90, 'confirmationStatus':'finalized', 'err':None}
    confirmed = {'slot':95, 'confirmationStatus':'confirmed', 'err':None}
    a = {'context':{'slot':100}, 'value':[final, None]}
    b = {'context':{'slot':101}, 'value':[None, confirmed]}
    result = await ExecutionRpc(Client(a), Client(b)).call('getSignatureStatuses', [['a','b'], {}])
    assert result == {'context':{'slot':101}, 'value':[final, confirmed]}


@pytest.mark.asyncio
async def test_null_status_with_backup_down_remains_unknown():
    unknown = {'context':{'slot':100}, 'value':[None]}
    assert await ExecutionRpc(Client(unknown), Client(RpcError('HTTP-503'))).call('getSignatureStatuses', [['a'], {}]) == unknown


@pytest.mark.asyncio
async def test_cancellation_never_broadcasts_on_backup():
    fallback = Client(100)
    with pytest.raises(asyncio.CancelledError):
        await ExecutionRpc(Client(asyncio.CancelledError()), fallback).call('getSlot', [])
    assert fallback.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True])
async def test_real_executor_keeps_one_durable_signature_after_ambiguous_send(config, store, recovered):
    _, _, cfg, route, oid, builder, key = await signed_request(replace(config,dry_run=False), store)
    cfg = replace(cfg, dry_run=False, wallet_keypair=key)
    def primary(method, params):
        if method == 'simulateTransaction':
            return {'context':{'slot':route.trade.slot}, 'value':{'err':None}}
        assert store.order(oid)['state'] == 'signed'
        assert store.order(oid)['raw'] == params[0]
        return asyncio.TimeoutError()
    def backup(method, params):
        assert method == 'sendTransaction'
        assert store.order(oid)['raw'] == params[0]
        return store.order(oid)['signature'] if recovered else RpcError('HTTP-503')
    a, b = Client(primary), Client(backup)
    router = ExecutionRpc(a, b)
    executor = Executor(cfg, store, router, Notices(), Priority(), builder=builder)
    await executor.buy(oid, route)
    row = store.order(oid)
    assert row['state'] == ('submitted' if recovered else 'unknown')
    assert row['raw'] and row['signature']
    assert [p for m,p in a.calls if m=='sendTransaction'] == [p for m,p in b.calls if m=='sendTransaction']
    # Unknown/expired cannot release the reservation or construct a replacement.
    unknown = {'context':{'slot':route.trade.slot+10}, 'value':[None]}
    executor.reconcile_rpc = ExecutionRpc(Client(lambda m,p:unknown if m=='getSignatureStatuses' else 1000))
    await executor.reconcile_once()
    assert store.order(oid)['signature'] == row['signature']
    assert store.order(oid)['state'] == 'unknown'
    assert len([m for m,p in a.calls if m=='sendTransaction']) == 1


@pytest.mark.asyncio
async def test_execution_failure_alert_requires_sustained_outage_and_resets(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('trade_execution.rpc.time.monotonic', lambda: now[0])
    notices = Notices()
    primary, fallback = Client(RpcError('HTTP-503')), Client(RpcError('HTTP-429'))
    rpc = ExecutionRpc(primary, fallback, notices=notices)
    for stamp in (100,160,161,180):
        now[0] = stamp
        with pytest.raises(RpcError):await rpc.call('getSlot', [])
    assert sum(bool(kw.get('alert')) for _,kw in notices.items) == 1
    primary.result = 100
    now[0] = 186  # Let the transport circuit breaker expire before probing recovery.
    assert await rpc.call('getSlot', []) == 100
    primary.result = RpcError('HTTP-503')
    now[0] = 200
    with pytest.raises(RpcError):await rpc.call('getSlot', [])
    assert sum(bool(kw.get('alert')) for _,kw in notices.items) == 1


@pytest.mark.asyncio
async def test_runtime_lanes_share_real_cooldown_but_isolate_activity(config, store, monkeypatch):
    from tests.test_funding_history_rpc import wired_service
    service, _, calls, reply = await wired_service(config, store, monkeypatch, 'fixture')
    service.priority.cooldown[service.config.rpc] = float('inf')
    service.priority.active = 1
    reader = service.execution_reads
    assert reader.providers[0].public.session is not service.background_rpc.public.session
    assert reader.providers[0].public.priority is not service.priority
    assert service.execution_rpc.providers[0].priority is not reader.providers[0].public.priority
    assert service.confirmation_rpc.public.priority is not service.priority
    async def result(self):
        return {'result':MAINNET if self.result==MAINNET else {'context':{'slot':100},'value':[None]}}
    monkeypatch.setattr(reply, 'json', result)
    response = await asyncio.wait_for(reader.call('getSignatureStatuses', [['a'],{}]), timeout=3)
    assert response['value'] == [None]
    assert calls[-1][0] == service.config.execution_fallback_url
