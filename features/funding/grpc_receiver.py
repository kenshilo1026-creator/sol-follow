"""Network-only child: no SQLite, transaction normalization, signing or trading."""
import asyncio
import base64
import json
import os
import sys
import time
from types import SimpleNamespace
import grpc
from chain_common.yellowstone import geyser_pb2 as pb
from features.funding.grpc_process import HEADER,MAX_FRAME,MAX_CONTROL,QUEUE_MESSAGES,QUEUE_BYTES,EXIT_REASONS
from features.funding.grpc_feed import channel_for,stream_call


def read_control():
    raw=sys.stdin.buffer.readline(MAX_CONTROL+1)
    if not raw:raise EOFError
    if len(raw)>MAX_CONTROL or not raw.endswith(b'\n'):raise RuntimeError('grpc-ipc-protocol')
    return json.loads(raw)


class Outbox:
    def __init__(self,write,*,messages=QUEUE_MESSAGES,maximum_bytes=QUEUE_BYTES):
        self.queue=asyncio.Queue(maxsize=messages)
        self.bytes=0;self.maximum_bytes=maximum_bytes;self.write=write

    def put(self,update,wall,mono):
        raw=update.SerializeToString()
        if not 0<len(raw)<=MAX_FRAME:raise RuntimeError('grpc-ipc-protocol')
        frame=HEADER.pack(len(raw),wall,mono)+raw
        if self.queue.full() or self.bytes+len(frame)>self.maximum_bytes:
            raise RuntimeError('grpc-ipc-backlog')
        self.bytes+=len(frame);self.queue.put_nowait(frame)

    async def run(self):
        while True:
            frame=await self.queue.get()
            try:await asyncio.to_thread(self.write,frame)
            finally:self.bytes-=len(frame)


def stdout_write(frame):
    sys.stdout.buffer.write(frame);sys.stdout.buffer.flush()


async def session(call,*,read=read_control,write=stdout_write,outbox=None):
    outbox=outbox or Outbox(write)
    changed=asyncio.Event();ping=asyncio.Event();latest=None
    async def commands():
        nonlocal latest
        while True:
            row=await asyncio.to_thread(read)
            request=pb.SubscribeRequest.FromString(base64.b64decode(row['subscription'],validate=True))
            # Full subscription snapshots may coalesce; never replay history.
            if request.HasField('from_slot'):raise RuntimeError('grpc-ipc-protocol')
            latest=request;changed.set()
    async def controls():
        nonlocal latest
        while True:
            await changed.wait();changed.clear()
            if ping.is_set():
                ping.clear()
                async with asyncio.timeout(5):await call.write(pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
            request,latest=latest,None
            if request is not None:
                async with asyncio.timeout(5):await call.write(request)
    async def receive():
        count=0
        while True:
            try:
                async with asyncio.timeout(30):update=await call.read()
            except TimeoutError:raise RuntimeError('grpc-idle-timeout') from None
            wall,mono=time.time(),time.monotonic()
            if update is grpc.aio.EOF:raise RuntimeError('grpc-ended')
            if update.WhichOneof('update_oneof')=='ping':ping.set();changed.set()
            elif update.WhichOneof('update_oneof')!='pong':outbox.put(update,wall,mono)
            count+=1
            if count%64==0:await asyncio.sleep(0)
    tasks=[asyncio.create_task(work()) for work in (commands,controls,receive,outbox.run)]
    try:
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:task.result()
            except TimeoutError:raise RuntimeError('grpc-write-timeout') from None
    finally:
        call.cancel()
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def run():
    row=await asyncio.to_thread(read_control)
    config=SimpleNamespace(grpc_endpoint=row['endpoint'],alchemy_key=row['key'])
    async with channel_for(config) as channel:await session(stream_call(channel,config))


def main():
    # Pipes can block a writer thread if the parent stops consuming. Always
    # exit the process after cancellation rather than joining that blocked
    # thread via asyncio.run's executor shutdown. OS closes both pipe ends.
    loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
    code=0
    try:loop.run_until_complete(run())
    except EOFError:pass  # Parent closed stdin or exited.
    except grpc.RpcError as exc:code=80+exc.code().value[0]
    except BaseException as exc:
        code=next((n for n,reason in EXIT_REASONS.items() if type(exc) is RuntimeError and exc.args==(reason,)),76)
    finally:os._exit(code)  # No provider exception or credential ever reaches stdout/stderr.


if __name__=='__main__':main()
