"""Read-only, separate-process gRPC latency probe; never normalize or submit trades."""
import argparse
import asyncio
from collections import Counter
import json
import time

import grpc
from chain_common.yellowstone import geyser_pb2 as pb
from features.audit.dev_audit import connect
from features.funding.grpc_feed import channel_for, stream_call, subscription
from share_common.config import load
from share_common.metrics import Metrics


def hotlist_snapshot(data):
    with connect(data/'funding.sqlite3') as db:
        if db is None:raise RuntimeError('funding-database-missing')
        return {row['wallet'] for row in db.execute('SELECT wallet FROM hotlist WHERE expires>?',(time.time(),))}


class Samples:
    def __init__(self):
        self.metrics=Metrics()
        self.counts=Counter()
        self.missing_timestamp=0
        self.clock_ahead=0
        self.last_slot=None

    def accept(self, update, now):
        kind=update.WhichOneof('update_oneof') or 'unknown'
        self.counts[kind]+=1
        if kind not in ('block_meta','transaction'):return
        value=getattr(update,kind)
        self.last_slot=value.slot
        created=None
        if update.HasField('created_at') and update.created_at.seconds>0:
            created=update.created_at.seconds+update.created_at.nanos/1e9
            age=now-created
            if age<0:self.clock_ahead+=1
            else:self.metrics.add(kind+'_source_age',age)
        else:self.missing_timestamp+=1
        if kind=='block_meta' and value.HasField('block_time') and value.block_time.timestamp>0:
            self.metrics.add('block_time_age',now-value.block_time.timestamp)
            if created is not None:self.metrics.add('source_block_age',created-value.block_time.timestamp)

    def report(self, elapsed, final=False):
        timing=self.metrics.snapshot()
        for name,row in timing.items():row['max_ms']=round(max(self.metrics.samples[name]),2)
        return {'event':'stream-probe','final':final,'elapsed_s':round(elapsed,1),
                'messages':dict(self.counts),'last_slot':self.last_slot,
                'missing_timestamp':self.missing_timestamp,'source_clock_ahead':self.clock_ahead,
                'timing_latest_1000':timing}


async def measure(call, config, addresses, seconds, emit):
    samples=Samples();ping=asyncio.Event();started=time.monotonic();tasks=[]
    async def write(request):
        async with asyncio.timeout(5):await call.write(request)
    async def receive():
        count=0
        while True:
            update=await call.read()
            if update is grpc.aio.EOF:raise RuntimeError('grpc-ended')
            samples.accept(update,time.time())
            if update.WhichOneof('update_oneof')=='ping':ping.set()
            count+=1
            if count%64==0:await asyncio.sleep(0)
    async def respond():
        while True:
            await ping.wait();ping.clear()
            await write(pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
    async def report():
        next_report=started+10
        while True:
            due=time.monotonic()+.25
            await asyncio.sleep(.25)
            now=time.monotonic()
            samples.metrics.add('event_loop_lag',max(0,now-due))
            if now>=next_report:
                emit(samples.report(now-started));next_report=now+10
    try:
        # Same hotlist and commitment as production; fixed snapshot for the run.
        # No replay, RPC fetches, database writes, decoding or trading.
        await write(subscription(addresses,commitment=config.hotlist_commitment))
        tasks=[asyncio.create_task(work()) for work in (receive,respond,report)]
        done,_=await asyncio.wait(tasks,timeout=seconds,return_when=asyncio.FIRST_COMPLETED)
        for task in done:task.result()
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        call.cancel()
        emit(samples.report(time.monotonic()-started,final=True))


async def run(config,seconds,emit):
    addresses=hotlist_snapshot(config.data)
    if not addresses:raise RuntimeError('hotlist-empty')
    if not config.alchemy_key:raise RuntimeError('alchemy-key-missing')
    emit({'event':'stream-probe-start','addresses':len(addresses),'commitment':config.hotlist_commitment,
          'duration_s':seconds,'fixed_hotlist':True})
    async with channel_for(config) as channel:
        await measure(stream_call(channel,config),config,addresses,seconds,emit)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=60)
    args=parser.parse_args()
    if not 10<=args.seconds<=300:parser.error('--seconds must be 10..300')
    emit=lambda row:print(json.dumps(row,ensure_ascii=False),flush=True)
    try:asyncio.run(run(load(),args.seconds,emit))
    except KeyboardInterrupt:pass
    except Exception as exc:
        # Provider exception text may include the endpoint API key.
        reason=str(exc) if type(exc) is RuntimeError and str(exc) in {
            'funding-database-missing','hotlist-empty','alchemy-key-missing','grpc-ended'} else type(exc).__name__
        row={'event':'stream-probe-error','reason':reason}
        if isinstance(exc,grpc.RpcError):row['status']=exc.code().name
        emit(row)
        raise SystemExit(1) from None


if __name__=='__main__':main()
