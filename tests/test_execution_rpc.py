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


def test_alchemy_primary_public_fallback_and_explicit_overrides(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    public='https://public.example'
    cfg=load(tmp_path,env={'ALCHEMY_API_KEY':'fixture-key','SOL_RPC_HTTP_URL':public})
    alchemy='https://solana-mainnet.g.alchemy.com/v2/fixture-key'
    payload=payload_for(cfg,{},'wallet')
    assert payload['rpc']==alchemy and payload['rpcFallback']==public
    assert cfg.execution_ws_url==cfg.ws
    assert 'fixture-key' not in repr(cfg)
    custom=replace(cfg,execution_http='https://dedicated.example')
    assert custom.execution_rpc_url=='https://dedicated.example'
    assert custom.execution_fallback_url==alchemy
    assert replace(cfg,execution_http=public).execution_fallback_url==alchemy
    assert replace(cfg,execution_fallback_http='https://backup.example').execution_fallback_url=='https://backup.example'
    assert replace(cfg,execution_fallback_http=alchemy).execution_fallback_url==''
    no_key=replace(cfg,alchemy_key='')
    assert no_key.execution_rpc_url==public and no_key.execution_fallback_url==''


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
@pytest.mark.parametrize('hedge_ms',[0,5])
async def test_real_executor_keeps_one_durable_signature_after_ambiguous_send(config, store, recovered, hedge_ms):
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
    router = ExecutionRpc(a, b, hedge_ms=hedge_ms)
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
    assert calls[-1][0] == service.config.execution_rpc_url


@pytest.mark.asyncio
async def test_hedge_fast_primary_avoids_backup(config,store):
    raw,signature,*_=await signed_request(config,store)
    backup=Client(signature)
    rpc=ExecutionRpc(Client(signature),backup,hedge_ms=50)
    assert await rpc.call('sendTransaction',[raw,{'encoding':'base64'}])==signature
    assert not backup.calls and rpc.snapshot()['send_hedges']==0
    assert rpc.snapshot()['send_primary_wins']==1


@pytest.mark.asyncio
async def test_hedge_slow_primary_cancelled_without_cooling_and_identical_bytes(config,store):
    raw,signature,*_=await signed_request(config,store)
    cancelled=asyncio.Event()
    class Slow(Client):
        async def call(self,method,params):
            if method=='getGenesisHash':return await super().call(method,params)
            self.calls.append((method,deepcopy(params)))
            try:await asyncio.Future()
            finally:cancelled.set()
    primary,backup=Slow(),Client(signature)
    rpc=ExecutionRpc(primary,backup,hedge_ms=5)
    params=[raw,{'encoding':'base64','skipPreflight':True,'minContextSlot':123,'maxRetries':0}]
    assert await asyncio.wait_for(rpc.call('sendTransaction',params),1)==signature
    assert primary.calls[-1]==backup.calls[-1]==('sendTransaction',params)
    assert cancelled.is_set() and not rpc.cooldown
    assert rpc.snapshot()['send_hedges']==rpc.snapshot()['send_fallback_wins']==1


@pytest.mark.asyncio
async def test_hedge_invalid_backup_does_not_cancel_primary(config,store):
    raw,signature,*_=await signed_request(config,store)
    release=asyncio.Event()
    class Primary(Client):
        async def call(self,method,params):
            if method!='getGenesisHash':await release.wait()
            return await super().call(method,params)
    class Backup(Client):
        async def call(self,method,params):
            if method!='getGenesisHash':release.set()
            return await super().call(method,params)
    rpc=ExecutionRpc(Primary(signature),Backup('wrong-signature'),hedge_ms=5)
    assert await asyncio.wait_for(rpc.call('sendTransaction',[raw,{'encoding':'base64'}]),1)==signature
    assert rpc.snapshot()['send_primary_wins']==1


@pytest.mark.asyncio
async def test_hedge_cancellation_cleans_up_both_requests(config,store):
    raw,signature,*_=await signed_request(config,store)
    started=[asyncio.Event(),asyncio.Event()];cancelled=[]
    class Slow(Client):
        def __init__(self,index):super().__init__();self.index=index
        async def call(self,method,params):
            if method=='getGenesisHash':return MAINNET
            started[self.index].set()
            try:await asyncio.Future()
            finally:cancelled.append(self.index)
    rpc=ExecutionRpc(Slow(0),Slow(1),hedge_ms=5)
    task=asyncio.create_task(rpc.call('sendTransaction',[raw,{'encoding':'base64'}]))
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert sorted(cancelled)==[0,1] and not rpc.cooldown


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked',['wrong-chain','cooling','429'])
async def test_hedge_preserves_provider_gates(config,store,blocked):
    import time
    raw,signature,*_=await signed_request(config,store)
    primary=Client(RpcError('HTTP-429') if blocked=='429' else signature,
                   genesis=str(Hash.new_unique()) if blocked=='wrong-chain' else MAINNET)
    rpc=ExecutionRpc(primary,Client(signature),hedge_ms=5)
    if blocked=='cooling':rpc.cooldown[0]=time.monotonic()+60
    assert await rpc.call('sendTransaction',[raw,{'encoding':'base64'}])==signature
    sends=[method for method,_ in primary.calls if method=='sendTransaction']
    assert len(sends)==(1 if blocked=='429' else 0)


def test_hedge_configuration_reaches_sdk_and_can_be_disabled(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    for value in [0,75,1000]:
        cfg=load(tmp_path,env={'SOL_EXECUTION_HEDGE_MS':str(value)})
        assert payload_for(cfg,{},'wallet')['rpcHedgeMs']==value
    assert load(tmp_path,env={}).execution_hedge_ms==75
    for value in ['-1','1001','nan']:
        with pytest.raises(ValueError):load(tmp_path,env={'SOL_EXECUTION_HEDGE_MS':value})
