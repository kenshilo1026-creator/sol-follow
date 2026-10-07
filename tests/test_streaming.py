"""Wire compatibility, durable inbox, routing isolation and event-only catch-up."""
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import struct
import time
import pytest
from chain_common.background_rpc import BackgroundRpc
from chain_common.primitives import b58encode,b58decode,SYSTEM
from chain_common.rpc import Priority,Rpc,RpcError
from chain_common.transaction import Tx,Unsupported
from chain_common.yellowstone import geyser_pb2 as pb
from chain_common.yellowstone.normalize import normalize
from features.funding.discovery import Discovery
from features.funding.grpc_feed import HotlistFeed,subscription
from features.runtime.service import Service
from features.database.storage import Store
from launchpads.create import decode as creates
from trade_execution.route import decode as route
from share_common.config import load
from tests.helpers import Notices,address

FIX=Path(__file__).parent/'fixtures'


def wire(raw):
    """Encode the public JSON fixtures into real upstream protobuf messages.

    Parsed non-transfer standard instructions are opaque no-ops in this helper;
    instructions decoded by this project retain their exact original bytes.
    """
    out=pb.SubscribeUpdate()
    value=out.transaction
    value.slot=raw['slot']
    info=value.transaction
    tx,meta=info.transaction,info.meta
    info.signature=b58decode(raw['transaction']['signatures'][0])
    tx.signatures.extend(b58decode(s) for s in raw['transaction']['signatures'])
    msg=raw['transaction']['message'];keys=msg['accountKeys']
    static=[k for k in keys if k.get('source','transaction')=='transaction']
    tx.message.account_keys.extend(b58decode(k['pubkey']) for k in static)
    h=tx.message.header
    h.num_required_signatures=sum(bool(k.get('signer')) for k in static)
    h.num_readonly_signed_accounts=sum(bool(k.get('signer')) and not k.get('writable') for k in static)
    h.num_readonly_unsigned_accounts=sum(not k.get('signer') and not k.get('writable') for k in static)
    tx.message.recent_blockhash=b58decode(msg.get('recentBlockhash',SYSTEM))
    tx.message.versioned=raw.get('version')!='legacy'
    if raw.get('version')==1:
        tx.message.config.SetInParent()
    for k in keys[len(static):]:
        target=meta.loaded_writable_addresses if k['writable'] else meta.loaded_readonly_addresses
        target.append(b58decode(k['pubkey']))
    for lookup in msg.get('addressTableLookups',[]):
        tx.message.address_table_lookups.add(account_key=b58decode(lookup['accountKey']),
            writable_indexes=bytes(lookup['writableIndexes']),readonly_indexes=bytes(lookup['readonlyIndexes']))
    names=[k['pubkey'] for k in keys]
    def ix(target,item):
        target.program_id_index=names.index(item['programId'])
        target.accounts=bytes(names.index(a) if isinstance(a,str) else a for a in item.get('accounts',[]))
        target.data=b58decode(item.get('data',''))
        if item.get('parsed',{}).get('type')=='transfer' and item['programId']==SYSTEM:
            data=item['parsed']['info']
            target.accounts=bytes([names.index(data['source']),names.index(data['destination'])])
            target.data=struct.pack('<IQ',2,int(data['lamports']))
        if item.get('parsed',{}).get('type')=='transferChecked':
            data=item['parsed']['info']
            target.accounts=bytes(names.index(data[k]) for k in ('source','mint','destination','authority'))
            target.data=struct.pack('<BQB',12,int(data['tokenAmount']['amount']),int(data['tokenAmount']['decimals']))
        if 'stack_height' in target.DESCRIPTOR.fields_by_name and item.get('stackHeight') is not None:
            target.stack_height=item['stackHeight']
    for item in msg['instructions']:
        ix(tx.message.instructions.add(),item)
    for group in raw['meta'].get('innerInstructions') or []:
        dest=meta.inner_instructions.add(index=group['index'])
        for item in group['instructions']:
            ix(dest.instructions.add(),item)
    meta.fee=raw['meta']['fee']
    meta.pre_balances.extend(raw['meta']['preBalances'])
    meta.post_balances.extend(raw['meta']['postBalances'])
    for phase,target in [('pre',meta.pre_token_balances),('post',meta.post_token_balances)]:
        for b in raw['meta'].get(phase+'TokenBalances') or []:
            row=target.add(account_index=b['accountIndex'],mint=b['mint'],owner=b.get('owner',''),program_id=b.get('programId',''))
            row.ui_token_amount.amount=b['uiTokenAmount']['amount']
            row.ui_token_amount.decimals=b['uiTokenAmount']['decimals']
    return pb.SubscribeUpdate.FromString(out.SerializeToString())


@pytest.mark.parametrize('filename',[p.name for p in FIX.glob('create_*.json') if p.name not in ('create_samples.json','create_abi_sources.json')])
def test_four_real_creates_survive_wire_normalization(filename):
    raw=json.loads((FIX/filename).read_text())
    converted=normalize(wire(raw).transaction,raw['blockTime'])
    assert converted['version']==raw['version']
    assert creates(Tx(converted))==creates(Tx(raw))
    assert creates(Tx(converted))


def test_real_buy_route_retains_alt_and_balances():
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text())
    assert route(Tx(normalize(wire(raw).transaction,raw['blockTime'])))==route(Tx(raw))


def test_10000_wallet_filter_is_bounded_and_not_all_transactions():
    wallets={b58encode(hashlib.sha256(str(i).encode()).digest()) for i in range(10000)}
    r=pb.SubscribeRequest.FromString(subscription(wallets,123).SerializeToString())
    assert set(r.transactions['hotlist_0'].account_include)==wallets
    assert r.transactions['hotlist_0'].HasField('failed') and not r.transactions['hotlist_0'].failed
    assert r.commitment==pb.CONFIRMED and r.from_slot==123
    assert not r.accounts and not r.blocks and not r.transactions_status
    assert not subscription([]).transactions


def test_key_only_selects_feed_and_never_changes_http(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    cfg=load(tmp_path,env={'ALCHEMY_API_KEY':'private-test-key'})
    assert cfg.feed_mode=='alchemy_grpc'
    assert cfg.rpc=='https://api.mainnet-beta.solana.com'
    assert cfg.ws=='wss://api.mainnet-beta.solana.com'
    assert 'private-test-key' not in repr(cfg)
    with pytest.raises(ValueError):load(tmp_path,env={'SOL_FEED_MODE':'alchemy_grpc'})


def test_payload_survives_restart_and_is_removed_after_completion(config,store):
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text());sig=raw['transaction']['signatures'][0]
    assert store.stream_transaction(raw)==1
    assert store.stream_transaction(raw)==0
    restarted=Store(config.data)
    assert restarted.stream_payload(sig)==raw
    assert restarted.due(1,time.time()+1,streamed=False)==[]
    assert restarted.due(1,time.time()+1,streamed=True)[0]['signature']==sig
    restarted.job_result(sig,'done')
    assert restarted.stream_payload(sig) is None
    assert restarted.stream_transaction(raw)==0 and restarted.stream_payload(sig) is None


@pytest.mark.asyncio
async def test_stream_payload_decodes_without_get_transaction(config,store):
    raw=json.loads((FIX/'create_CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump.json').read_text())
    raw['blockTime']=int(time.time());store.stream_transaction(raw)
    class NoHTTP:
        async def call(self,*a):raise AssertionError('unexpected HTTP')
        async def transaction(self,*a):raise AssertionError('unexpected getTransaction')
    service=Service(config,store);service.notices=Notices()
    await service.process(store.due(1,time.time()+1)[0],NoHTTP())
    assert len(store.rows('funding','SELECT * FROM launch_creates'))==1


@pytest.mark.asyncio
async def test_no_background_network_when_idle_then_one_event_catchup(config,store):
    class Public:
        def __init__(self):self.calls=[]
        async def call(self,m,p):self.calls.append((m,p));return []
    rpc=Public();d=Discovery(config,store,rpc,Notices())
    task=asyncio.create_task(d.urgent_history())
    try:
        await asyncio.sleep(0.05);assert rpc.calls==[]
        d.request_history(next(iter(config.cex)))
        async with asyncio.timeout(2):
            while not rpc.calls:await asyncio.sleep(0.01)
        await asyncio.sleep(0.6)
        assert len(rpc.calls)==1
        assert store.rows('funding','SELECT * FROM history_requests')==[]
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_public_limiter_has_no_paid_fallback():
    class Public:
        async def call(self,m,p):raise RpcError('HTTP-429')
    rpc=BackgroundRpc(Public(),Priority(),2)
    with pytest.raises(RpcError,match='429'):await rpc.call('getTransaction',[])
    with pytest.raises(ValueError):await rpc.call('sendTransaction',[])


def test_source_discovery_excludes_hotlist_in_grpc_mode(config,store):
    from tests.test_signals import fund
    wallet=address();fund(store,wallet,time.time())
    d=Discovery(config,store,None,Notices(),sources_only=True);d.refresh()
    assert wallet not in d.addresses
    f=HotlistFeed(config,store,None,Notices(),d)
    assert f.addresses()=={wallet}


@pytest.mark.asyncio
async def test_replay_overlap_and_expired_gap(config,store):
    class Public:
        head=1000
        async def call(self,*args):return self.head
    rpc=Public();d=Discovery(config,store,None,Notices());f=HotlistFeed(config,store,rpc,Notices(),d)
    f.last_slot=900
    assert await f.resume_slot({'a'}) is None
    assert f.live_floor==1001
    rpc.head=9000
    assert await f.resume_slot({'a'}) is None
    assert store.rows('funding','SELECT address FROM history_requests')==[]
    assert f.live_floor==9001


def test_checkpoint_does_not_advance_when_inbox_commit_fails(config,store,monkeypatch):
    d=Discovery(config,store,None,Notices());f=HotlistFeed(config,store,None,Notices(),d)
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text())
    def fail(*args):raise OSError('disk')
    monkeypatch.setattr(store,'stream_transaction',fail)
    with pytest.raises(OSError):f.accept(wire(raw))
    assert store.stream_state('grpc_slot') is None and f.last_slot==0


def test_history_request_added_during_processing_is_not_lost(store):
    store.request_history('a');row=store.rows('funding','SELECT * FROM history_requests')[0]
    store.request_history('a');store.finish_history(row)
    assert len(store.rows('funding','SELECT * FROM history_requests'))==1


@pytest.mark.asyncio
async def test_real_grpc_transport_dynamic_filter_and_durable_receive(config,store):
    import grpc
    from tests.test_signals import fund
    seen=[];ready=asyncio.Event();changed=asyncio.Event()
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text())
    wallet=raw['transaction']['message']['accountKeys'][0]['pubkey']
    fund(store,wallet,time.time())
    second=address()
    cfg=replace(config,alchemy_key='test-only-key',feed_mode='alchemy_grpc')
    async def server_stream(requests,context):
        assert dict(context.invocation_metadata())['x-token']=='test-only-key'
        async for request in requests:
            if request.HasField('ping'):continue
            seen.append(set(a for f in request.transactions.values() for a in f.account_include))
            if len(seen)==1:
                assert request.from_slot==raw['slot']-128
                meta=pb.SubscribeUpdate()
                meta.block_meta.slot=raw['slot']
                meta.block_meta.block_time.timestamp=raw['blockTime']
                yield meta
                yield wire(raw)
                ready.set()
            else:
                assert second in seen[-1]
                changed.set()
                yield pb.SubscribeUpdate(pong=pb.SubscribeUpdatePong(id=1))
    server=grpc.aio.server()
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser',{
        'Subscribe':grpc.stream_stream_rpc_method_handler(server_stream,
            request_deserializer=pb.SubscribeRequest.FromString,response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
    port=server.add_insecure_port('127.0.0.1:0');await server.start()
    d=Discovery(cfg,store,None,Notices(),sources_only=True)
    f=HotlistFeed(cfg,store,None,Notices(),d)
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            task=asyncio.create_task(f.connection(channel,{wallet},raw['slot']-128))
            try:
                await asyncio.wait_for(ready.wait(),3)
                async with asyncio.timeout(3):
                    while not store.stream_payload(raw['transaction']['signatures'][0]):await asyncio.sleep(0.01)
                assert store.stream_state('grpc_slot')==raw['slot']
                assert f.connected and f.received==1
                fund(store,second,time.time())
                await asyncio.wait_for(changed.wait(),4)
                assert seen==[{wallet},{wallet,second}]
                assert store.rows('funding','SELECT * FROM history_requests')==[]
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
    finally:
        await server.stop(0)
