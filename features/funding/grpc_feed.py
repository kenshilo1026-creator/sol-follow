"""Alchemy hotlist-only stream with public-first/Alchemy-fallback head reads.

The durable inbox is committed before advancing the checkpoint. Hotlist changes
subscribe to live traffic only; they never queue public history scans.
"""
import asyncio
import time
from urllib.parse import urlparse
from collections import Counter, deque
from share_common.metrics import Metrics
import grpc
from chain_common.availability import Availability
from features.audit.feed_trace import observed
from chain_common.primitives import b58encode
from chain_common.transaction import Unsupported
from chain_common.yellowstone import geyser_pb2 as pb
from chain_common.yellowstone.normalize import normalize


# Stream health is independent of the trading signal deadline. Block timestamps
# are whole seconds and are not a measurement of transport latency.
STALE_BLOCK_SECONDS = 10
STALE_PERSIST_SECONDS = 3
STALE_HARD_SECONDS = 30
CONTROL_WRITE_TIMEOUT_SECONDS = 5


def failure_reason(exc):
    # Never expose arbitrary exception text: endpoints and credentials may occur
    # in transport errors. Only our exact, fixed local reason codes are safe.
    if isinstance(exc,RuntimeError) and len(exc.args)==1:
        reason=exc.args[0]
        if isinstance(reason,str) and reason in {
            'grpc-stale-block-time','grpc-ended','grpc-idle-timeout','grpc-write-timeout',
        }:return reason
    return 'grpc-transport-error' if isinstance(exc,grpc.RpcError) else 'local-error'


def subscription(addresses, from_slot=None, chunk_size=10000, commitment="confirmed"):
    request = pb.SubscribeRequest(commitment=pb.PROCESSED if commitment=="processed" else pb.CONFIRMED)
    if commitment=="processed":
        request.slots["forks"].filter_by_commitment=False
        request.slots["forks"].interslot_updates=True
    addresses = sorted(set(addresses))
    for start in range(0,len(addresses),chunk_size):
        f = request.transactions['hotlist_'+str(start//chunk_size)]
        f.vote = False
        f.failed = False
        f.account_include.extend(addresses[start:start+chunk_size])
    # Small block metadata supplies chain timestamps; never subscribe to blocks
    # or unrestricted transactions. Empty hotlists do not open a stream at all.
    request.blocks_meta['clock'].SetInParent()
    if from_slot is not None:
        request.from_slot = from_slot
    return request


def channel_for(config):
    parsed = urlparse(config.grpc_endpoint)
    target = parsed.netloc
    return grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=[
        ('grpc.max_receive_message_length',16*1024*1024),
        ('grpc.keepalive_time_ms',20000), ('grpc.keepalive_timeout_ms',10000)])


def stream_call(channel, config):
    return channel.stream_stream('/geyser.Geyser/Subscribe',
        request_serializer=pb.SubscribeRequest.SerializeToString,
        response_deserializer=pb.SubscribeUpdate.FromString)(
            metadata=(('x-token',config.alchemy_key),))


class HotlistFeed:
    def __init__(self, config, store, rpc, notices, catchup):
        self.config,self.store,self.rpc,self.notices,self.catchup = config,store,rpc,notices,catchup
        self.connected = False
        self.subscribed = set()
        self.received = self.added = self.bytes_received = self.reconnects = self.dropped = 0
        self.last_slot = int(store.stream_state('grpc_slot',0))
        self.live_floor = self.last_slot+1
        self.anchor = None
        self.stale_since = None
        self.health=Availability(notices,'grpc')
        self.drop_reasons=Counter()
        self.delivery=Metrics()
        self.last_transaction_slot=None
        self.last_delivery_at=None
        self.last_source_age=None
        self.source_clock_ahead=0
        self.read_ready_at=None
        self.loop_due=None
        self.loop_delays=deque(maxlen=240)

    def addresses(self):
        return set(self.store.hotlist(time.time()))

    async def resume_slot(self, addresses):
        commitment=self.config.hotlist_commitment
        self.anchor=None
        self.stale_since=None
        head = await self.rpc.call('getSlot',[{'commitment':commitment}])
        self.live_floor=max(head,self.last_slot)+1
        # Live-only buying deliberately skips trades during disconnection.
        return None

    def check_freshness(self, update):
        if update.WhichOneof('update_oneof')!='block_meta':return
        block=update.block_meta
        if not block.HasField('block_time') or block.block_time.timestamp<=0:return
        received_at=time.time()
        lag=received_at-block.block_time.timestamp
        if lag<=STALE_BLOCK_SECONDS:
            self.stale_since=None
            return
        now=time.monotonic()
        if self.stale_since is None:self.stale_since=now
        duration=now-self.stale_since
        if lag>STALE_HARD_SECONDS or duration>=STALE_PERSIST_SECONDS:
            # Inspect THIS rejected message: accept() has not run yet, so its
            # last_source_age belongs to a previous message. Provider-created
            # age includes transport/client scheduling and clock offset; it is
            # not a claim that either the network or the provider is at fault.
            diagnostic={}
            if self.read_ready_at is not None:
                diagnostic['read_ready_wait_ms']=round(max(0,now-self.read_ready_at)*1000,2)
            if self.loop_due is not None:
                diagnostic['event_loop_lag_max_ms']=round(1000*max(
                    [max(0,now-self.loop_due)]+[delay for stamp,delay in self.loop_delays if now-stamp<=60]),2)
            if update.HasField('created_at') and update.created_at.seconds>0:
                created=update.created_at.seconds+update.created_at.nanos/1e9
                diagnostic.update(source_age_s=round(received_at-created,3),
                                  source_block_lag_s=round(created-block.block_time.timestamp,3))
            timing=self.delivery.snapshot()
            for kind in ('block_meta','transaction'):
                row=timing.get(kind+'_local_accept')
                if row:diagnostic[kind+'_accept_p95_ms']=row['p95_ms']
            self.notices.emit('hotlist stream stale; reconnecting',
                {'slot':block.slot,'lag_seconds':round(lag,3),
                 'maximum_seconds':STALE_BLOCK_SECONDS,'stale_for_seconds':round(duration,3),
                 'hard_maximum_seconds':STALE_HARD_SECONDS,**diagnostic},
                alert=True,key='grpc-stale',interval=60)
            raise RuntimeError('grpc-stale-block-time')

    def live_reason(self, slot, now):
        a=self.anchor
        if not a:return 'missing-block-anchor'
        if slot<self.live_floor:return 'before-live-floor'
        if slot<a[0]:return 'before-anchor-slot'
        if slot>a[0]+8:return 'ahead-of-anchor-window'
        if now<a[2]:return 'anchor-receipt-in-future'
        if now-a[2]>2:return 'anchor-receipt-expired'
        if now-a[1]<-2:return 'block-time-in-future'
        if now-a[1]>self.config.signal_age:return 'block-time-expired'
        return 'live'

    def live_slot(self, slot, now):
        return self.live_reason(slot,now)=='live'

    def snapshot(self):
        now=time.time()
        return {'connected':self.connected,'addresses':len(self.subscribed),
            'received':self.received,'new_jobs':self.added,'protobuf_bytes':self.bytes_received,
            'last_slot':self.last_slot,'reconnects':self.reconnects,'dropped_nonlive':self.dropped,
            'live_floor':self.live_floor,'drop_reasons':dict(self.drop_reasons),
            'block_time_lag_s':round(now-self.anchor[1],3) if self.anchor else None,
            'anchor_slot':self.anchor[0] if self.anchor else None,
            'anchor_received_age_s':round(now-self.anchor[2],3) if self.anchor else None,
            'last_transaction_slot':self.last_transaction_slot,
            'last_delivery_age_s':round(now-self.last_delivery_at,3) if self.last_delivery_at is not None else None,
            'last_source_age_s':round(self.last_source_age,3) if self.last_source_age is not None else None,
            'source_clock_ahead':self.source_clock_ahead,'delivery_timing':self.delivery.snapshot()}

    def accept(self, update):
        began=time.monotonic()
        now=time.time()
        kind=update.WhichOneof('update_oneof')
        if kind in ('transaction','block_meta'):
            self.last_delivery_at=now
            self.last_source_age=None
            if update.HasField('created_at') and update.created_at.seconds>0:
                stamp=update.created_at.seconds+update.created_at.nanos/1e9
                self.last_source_age=now-stamp
                # This includes upstream queues, transport and client scheduling;
                # the provider timestamp is not a pure network latency clock.
                if self.last_source_age>=0:
                    self.delivery.add(kind+'_source_age',self.last_source_age)
                else:self.source_clock_ahead+=1
        try:
            self._accept(update)
        finally:
            if kind in ('transaction','block_meta'):
                self.delivery.add(kind+'_local_accept',time.monotonic()-began)

    def _accept(self, update):
        self.bytes_received += update.ByteSize()
        kind = update.WhichOneof('update_oneof')
        slot = 0
        if kind == 'slot' and update.slot.status==pb.SLOT_DEAD:
            from features.funding.processed import Processed
            count=Processed(self.store).dead_slot(update.slot.slot)
            if count:
                self.notices.emit('processed slot dropped; votes revoked', {'slot':update.slot.slot,'signals':count},alert=True)
        elif kind == 'block_meta':
            block = update.block_meta
            slot = block.slot
            if block.HasField('block_time') and block.block_time.timestamp > 0:
                self.store.block_time(slot,block.block_time.timestamp)
                self.anchor=(slot,block.block_time.timestamp,time.time())
        elif kind == 'transaction':
            received_at=time.time()
            value = update.transaction
            slot = value.slot
            self.received += 1
            self.last_transaction_slot=slot
            live_reason=self.live_reason(slot,received_at)
            live=live_reason=='live'
            if not live:self.drop_reasons[live_reason]+=1
            try:
                raw = normalize(value,self.store.block_time(slot))
            except (Unsupported,ValueError,IndexError) as exc:
                # A schema/metadata incompatibility is fetched once via PUBLIC
                # RPC instead of dropping the event or fabricating its contents.
                if len(value.transaction.signature) != 64:
                    raise Unsupported('stream-invalid-signature') from None
                signature=b58encode(value.transaction.signature)
                observed(self.store,signature,'grpc-'+self.config.hotlist_commitment,'received',slot,received_at,
                         normalized=False,live=live,dropped=not live,live_reason=live_reason,error=type(exc).__name__)
                if live:self.added += self.store.enqueue([signature])
                else:self.dropped += 1
                self.notices.emit('stream transaction requires public RPC decoding',
                    {'slot':slot,'type':type(exc).__name__},key='grpc-normalize')
            else:
                now=time.time(); a=self.anchor
                observed(self.store,raw['transaction']['signatures'][0],'grpc-'+self.config.hotlist_commitment,
                         'received',slot,received_at,normalized=True,live=live,dropped=not live,
                         live_reason=live_reason,source_age_s=self.last_source_age,
                         live_floor=self.live_floor,anchor_slot=a[0] if a else None,
                         anchor_age_s=now-a[2] if a else None,
                         block_time_lag_s=now-a[1] if a else None)
                raw['_stream']={'commitment':self.config.hotlist_commitment,'live':live,'source':'grpc'}
                if live:self.added += self.store.stream_transaction(raw)
                else:self.dropped += 1
        if slot > self.last_slot:
            # Only reached once the incoming transaction (if any) is durable.
            self.store.set_stream_state('grpc_slot',int(slot))
            self.last_slot = slot

    async def watch_loop(self):
        self.loop_delays.clear()
        try:
            while True:
                self.loop_due=time.monotonic()+.25
                await asyncio.sleep(.25)
                now=time.monotonic()
                self.loop_delays.append((now,max(0,now-self.loop_due)))
        finally:self.loop_due=None

    async def write_control(self, call, request):
        began=time.monotonic()
        try:
            async with asyncio.timeout(CONTROL_WRITE_TIMEOUT_SECONDS):
                await call.write(request)
        except TimeoutError:
            raise RuntimeError('grpc-write-timeout') from None
        finally:self.delivery.add('control_write',time.monotonic()-began)

    async def controls(self, call, addresses, ping):
        # One writer serializes pings/filter updates while reads keep draining.
        refresh_at=time.monotonic()+2
        while True:
            try:await asyncio.wait_for(ping.wait(),timeout=max(0,refresh_at-time.monotonic()))
            except asyncio.TimeoutError:pass
            if ping.is_set():
                ping.clear()
                await self.write_control(call,pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
            if time.monotonic()>=refresh_at:
                desired=self.addresses()
                if not desired:return
                if desired!=addresses:
                    await self.write_control(call,subscription(desired,commitment=self.config.hotlist_commitment))
                    addresses=desired
                    self.subscribed=set(desired)
                    self.store.set_stream_state('grpc_watch',sorted(desired))
                refresh_at=time.monotonic()+2

    async def connection(self, channel, addresses, start):
        call = stream_call(channel,self.config)
        try:
            await self.write_control(call,subscription(addresses,start,commitment=self.config.hotlist_commitment))
            self.store.set_stream_state('grpc_watch',sorted(addresses))
            self.subscribed = set(addresses)
            last_update = time.monotonic()
            connected_at = time.monotonic()
            # Do not cancel call.read() just to refresh filters: cancelling an
            # aio gRPC read cancels the whole stream.
            async def receive():
                update=await call.read()
                return update,time.monotonic()
            self.read_ready_at=None
            ping=asyncio.Event()
            controls=asyncio.create_task(self.controls(call,addresses,ping))
            watchdog=asyncio.create_task(self.watch_loop())
            read = asyncio.create_task(receive())
            try:
                while True:
                    done,_ = await asyncio.wait({read,controls},timeout=1,return_when=asyncio.FIRST_COMPLETED)
                    if controls in done:
                        controls.result()
                        return
                    if done:
                        update,self.read_ready_at = read.result()
                        if update is grpc.aio.EOF:
                            raise RuntimeError('grpc-ended')
                        last_update = time.monotonic()
                        self.connected = True
                        if self.health.since is not None and time.monotonic()-connected_at>=30:
                            self.health.recovered('hotlist stream recovered')
                        if update.WhichOneof('update_oneof') == 'ping':
                            ping.set()
                        else:
                            self.check_freshness(update)
                            self.accept(update)
                        read = asyncio.create_task(receive())
                    if time.monotonic()-last_update > 30:
                        raise RuntimeError('grpc-idle-timeout')
            finally:
                for task in (read,controls,watchdog):task.cancel()
                await asyncio.gather(read,controls,watchdog,return_exceptions=True)
                self.read_ready_at=None
        finally:
            call.cancel()

    async def run(self):
        backoff = 1
        while True:
            addresses = self.addresses()
            if not addresses:
                self.connected = False
                self.subscribed.clear()
                await asyncio.sleep(1)
                continue
            began = time.monotonic()
            stage = 'getSlot'
            retry_delay = backoff
            try:
                start = await self.resume_slot(addresses)
                stage = 'grpc-subscription'
                async with channel_for(self.config) as channel:
                    await self.connection(channel,addresses,start)
                retry_delay = 1 if time.monotonic()-began > 30 else backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.code().name if isinstance(exc,grpc.RpcError) else type(exc).__name__
                reason = failure_reason(exc)
                retry_delay = 1 if time.monotonic()-began > 30 else backoff
                # A local stale-stream restart should not create a minute-long
                # blind period. Transport/rate-limit failures retain full backoff.
                if reason=='grpc-stale-block-time':retry_delay=min(5,retry_delay)
                self.health.failure('hotlist Alchemy stream unavailable; retrying live subscription',
                    {'status':status,'reason':reason,'stage':stage,'retry_s':retry_delay})
                self.reconnects += 1
            finally:
                self.connected = False
                self.subscribed.clear()
            await asyncio.sleep(retry_delay)
            backoff = min(60,retry_delay*2)
