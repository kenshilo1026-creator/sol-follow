"""Offline child-process transport, backpressure, timestamps and lifecycle."""
import asyncio
import base64
from dataclasses import replace
import json
import os
import queue
import sys
import threading
import time

import grpc
import pytest

from chain_common.yellowstone import geyser_pb2 as pb
from features.funding.grpc_feed import HotlistFeed,subscription,failure_reason
from features.funding.grpc_process import ReceiverProcess,HEADER,MAX_FRAME,RemoteRpcError,child_environment
from features.funding.grpc_receiver import Outbox,session
from tests.helpers import Notices
from tests.test_streaming import wire,FIX


def update(slot=123):
    value=pb.SubscribeUpdate()
    value.block_meta.slot=slot
    value.block_meta.block_time.timestamp=int(time.time())
    return value


def test_outbox_bounds_count_bytes_and_order():
    box=Outbox(None,messages=2)
    box.put(update(1),1,1);box.put(update(2),2,2)
    with pytest.raises(RuntimeError,match='grpc-ipc-backlog'):box.put(update(3),3,3)
    for slot in (1,2):
        frame=box.queue.get_nowait()
        size,wall,mono=HEADER.unpack(frame[:HEADER.size])
        assert size==len(frame)-HEADER.size and wall==mono==slot
        assert pb.SubscribeUpdate.FromString(frame[HEADER.size:]).block_meta.slot==slot
    box=Outbox(None,maximum_bytes=HEADER.size)
    with pytest.raises(RuntimeError,match='grpc-ipc-backlog'):box.put(update(),1,1)
    assert box.bytes==0 and box.queue.empty()


@pytest.mark.asyncio
async def test_child_receives_ping_and_updates_filters_while_ipc_writer_is_blocked():
    commands=queue.Queue();incoming=asyncio.Queue();writes=[];frames=[]
    blocked=threading.Event();release=threading.Event()
    class Call:
        cancelled=False
        async def read(self):return await incoming.get()
        async def write(self,value):writes.append(value)
        def cancel(self):self.cancelled=True
    call=Call()
    def read():
        value=commands.get()
        if value is None:raise EOFError
        return value
    def write(frame):
        blocked.set();release.wait(5);frames.append(frame)
    def send(wallet):
        commands.put({'subscription':base64.b64encode(subscription({wallet}).SerializeToString()).decode()})
    task=asyncio.create_task(session(call,read=read,write=write))
    try:
        send('first');await incoming.put(update(1))
        assert await asyncio.to_thread(blocked.wait,2)
        send('second')
        await incoming.put(pb.SubscribeUpdate(ping=pb.SubscribeUpdatePing()))
        await incoming.put(update(2))
        async with asyncio.timeout(2):
            while not (any(w.HasField('ping') for w in writes) and
                       any('second' in f.account_include for w in writes for f in w.transactions.values())):
                await asyncio.sleep(.01)
        assert not task.done() and not frames
        release.set()
        async with asyncio.timeout(2):
            while len(frames)<2:await asyncio.sleep(.01)
        assert [pb.SubscribeUpdate.FromString(f[HEADER.size:]).block_meta.slot for f in frames]==[1,2]
    finally:
        release.set();commands.put(None);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    assert call.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize('shutdown',['cancel','stdin-eof'])
async def test_real_child_transport_timestamp_ping_filter_and_cleanup(config,shutdown):
    # Only test injection replaces TLS with a loopback channel; production uses TLS.
    code="import grpc; from features.funding import grpc_receiver as r; r.channel_for=lambda c:grpc.aio.insecure_channel(c.grpc_endpoint); r.main()"
    initial=asyncio.Event();ping=asyncio.Event();changed=asyncio.Event();seen=[]
    async def handler(requests,context):
        assert dict(context.invocation_metadata())['x-token']=='offline-test-key'
        async for req in requests:
            if req.HasField('ping'):ping.set();continue
            assert not req.HasField('from_slot') and req.commitment==pb.PROCESSED
            seen.append({a for f in req.transactions.values() for a in f.account_include})
            yield update(len(seen))
            if len(seen)==1:
                yield pb.SubscribeUpdate(ping=pb.SubscribeUpdatePing())
                initial.set()
            else:changed.set()
    server=grpc.aio.server()
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser',{
        'Subscribe':grpc.stream_stream_rpc_method_handler(handler,
            request_deserializer=pb.SubscribeRequest.FromString,response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
    port=server.add_insecure_port('127.0.0.1:0');await server.start()
    cfg=replace(config,grpc_endpoint=f'127.0.0.1:{port}',alchemy_key='offline-test-key')
    receiver=ReceiverProcess(cfg,command=[sys.executable,'-B','-c',code])
    try:
        async with receiver:
            assert receiver.process.pid!=os.getpid()
            await receiver.write(subscription({'first'},commitment='processed'))
            await asyncio.wait_for(initial.wait(),5)
            await asyncio.wait_for(ping.wait(),3)  # Parent has not consumed a single update.
            before=time.time()
            time.sleep(.15)  # Stall the main event loop, not just one task.
            value=await asyncio.wait_for(receiver.read(),3)
            assert value.block_meta.slot==1 and receiver.received_at<=before
            assert receiver.queue_ms>=100
            await receiver.write(subscription({'first','second'},commitment='processed'))
            await asyncio.wait_for(changed.wait(),3)
            assert (await asyncio.wait_for(receiver.read(),3)).block_meta.slot==2
            assert seen==[{'first'},{'first','second'}]
            if shutdown=='stdin-eof':
                receiver.process.stdin.close()
                assert await asyncio.wait_for(receiver.process.wait(),3)==0
    finally:
        await receiver.close();await server.stop(0)
    assert receiver.process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected',[
    ('stale','grpc-ipc-backlog'),('invalid','grpc-ipc-protocol'),
    ('oversized','grpc-ipc-protocol'),('truncated','grpc-receiver-error'),
    ('overflow','grpc-ipc-backlog'),('denied','UNAUTHENTICATED'),
])
async def test_child_failure_frames_are_rejected_and_reaped(config,mode,expected):
    code="""
import os,sys,time
from chain_common.yellowstone import geyser_pb2 as pb
from features.funding.grpc_process import HEADER,MAX_FRAME
sys.stdin.buffer.readline()
mode=sys.argv[1]
if mode=='overflow':os._exit(73)
if mode=='denied':os._exit(96)
raw=pb.SubscribeUpdate(slot=pb.SubscribeUpdateSlot(slot=1)).SerializeToString()
if mode=='invalid':raw=bytes([255])
size=MAX_FRAME+1 if mode=='oversized' else len(raw)
sys.stdout.buffer.write(HEADER.pack(size,time.time(),time.monotonic()-(3 if mode=='stale' else 0)))
if mode=='truncated':sys.stdout.buffer.flush();os._exit(0)
sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
time.sleep(30)
"""
    receiver=ReceiverProcess(replace(config,alchemy_key='offline-test'),command=[sys.executable,'-B','-c',code,mode])
    async with receiver:
        if mode=='denied':
            with pytest.raises(RemoteRpcError) as exc:await asyncio.wait_for(receiver.read(),5)
            assert exc.value.code().name==expected
            assert failure_reason(exc.value)=='grpc-transport-error'
        else:
            with pytest.raises(RuntimeError,match=expected):await asyncio.wait_for(receiver.read(),5)
    assert receiver.process.returncode is not None


@pytest.mark.asyncio
async def test_close_drains_full_stdout_and_reaps_child(config):
    code="import sys,time; sys.stdin.buffer.readline(); sys.stdout.buffer.write(b'x'*2000000); sys.stdout.buffer.flush(); time.sleep(30)"
    receiver=ReceiverProcess(config,command=[sys.executable,'-B','-c',code])
    await receiver.__aenter__()
    await asyncio.sleep(.3)
    await asyncio.wait_for(receiver.close(),5)
    assert receiver.process.returncode is not None


def test_child_does_not_inherit_private_environment(monkeypatch):
    for name in ('SOL_PRIVATE_KEY','ALCHEMY_API_KEY','TELEGRAM_BOT_TOKEN','AWS_SECRET_ACCESS_KEY','PYTHONPATH'):
        monkeypatch.setenv(name,'do-not-inherit')
        assert name not in child_environment()


def test_parent_preserves_receipt_but_checks_freshness_at_processing_time(config,store,monkeypatch):
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text())
    now=time.time();received=now-.5
    feed=HotlistFeed(config,store,None,Notices(),None)
    feed.ingress_received_at=received;feed.ipc_queue_ms=500
    meta=update(raw['slot']);meta.block_meta.block_time.timestamp=int(now)
    feed.accept(meta)
    assert feed.anchor[2]==received
    saved=[]
    monkeypatch.setattr(store,'stream_transaction',lambda raw:saved.append(raw) or 1)
    feed.accept(wire(raw))
    assert saved[0]['_stream']['received_at']==received
    feed.ingress_received_at=now-3
    feed.anchor=(raw['slot'],now,now-3)
    feed.accept(wire(raw))
    assert len(saved)==1 and feed.drop_reasons['anchor-receipt-expired']==1


@pytest.mark.asyncio
async def test_service_feed_run_spawns_receiver_and_commits_before_checkpoint(config,store,monkeypatch):
    from features.funding import grpc_feed
    from tests.test_signals import fund
    raw=json.loads((FIX/'pump_non_sol_buy.json').read_text())
    wallet=raw['transaction']['message']['accountKeys'][0]['pubkey']
    fund(store,wallet,time.time())
    async def handler(requests,context):
        async for req in requests:
            if req.HasField('ping'):continue
            assert req.commitment==pb.PROCESSED and not req.HasField('from_slot')
            assert wallet in req.transactions['hotlist_0'].account_include
            yield update(raw['slot'])
            yield wire(raw)
    server=grpc.aio.server()
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser',{
        'Subscribe':grpc.stream_stream_rpc_method_handler(handler,
            request_deserializer=pb.SubscribeRequest.FromString,response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
    port=server.add_insecure_port('127.0.0.1:0');await server.start()
    children=[]
    class LoopbackReceiver(ReceiverProcess):
        def __init__(self,config):
            code="import grpc; from features.funding import grpc_receiver as r; r.channel_for=lambda c:grpc.aio.insecure_channel(c.grpc_endpoint); r.main()"
            super().__init__(config,command=[sys.executable,'-B','-c',code])
            children.append(self)
    class Head:
        async def call(self,*args):return raw['slot']-1
    monkeypatch.setattr(grpc_feed,'ReceiverProcess',LoopbackReceiver)
    cfg=replace(config,grpc_endpoint=f'127.0.0.1:{port}',alchemy_key='offline-test',hotlist_commitment='processed')
    feed=HotlistFeed(cfg,store,Head(),Notices(),None)
    task=asyncio.create_task(feed.run())
    try:
        async with asyncio.timeout(6):
            while not store.stream_payload(raw['transaction']['signatures'][0]):
                if task.done():task.result()
                await asyncio.sleep(.01)
        assert feed.snapshot()['receiver_pid']==children[0].process.pid
        assert feed.snapshot()['ipc_queue_ms'] is not None
        assert store.stream_state('grpc_slot')==raw['slot']
        assert feed.received==feed.added==1
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await server.stop(0)
    assert len(children)==1 and children[0].process.returncode is not None
    assert feed.receiver_pid is None and not feed.connected
