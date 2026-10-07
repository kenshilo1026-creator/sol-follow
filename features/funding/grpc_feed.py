"""Alchemy hotlist-only executed transaction stream; no paid HTTP calls.

The durable inbox is committed before advancing the checkpoint. Hotlist changes
subscribe to live traffic only; they never queue public history scans.
"""
import asyncio
import time
from urllib.parse import urlparse
import grpc
from chain_common.primitives import b58encode
from chain_common.transaction import Unsupported
from chain_common.yellowstone import geyser_pb2 as pb
from chain_common.yellowstone.normalize import normalize


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
        self.received = self.added = self.bytes_received = self.reconnects = 0
        self.last_slot = int(store.stream_state('grpc_slot',0))
        self.live_floor = self.last_slot+1
        self.anchor = None

    def addresses(self):
        return set(self.store.hotlist(time.time()))

    async def resume_slot(self, addresses):
        commitment=self.config.hotlist_commitment
        self.anchor=None
        head = await self.rpc.call('getSlot',[{'commitment':commitment}])
        self.live_floor=max(head,self.last_slot)+1
        # Live-only buying deliberately skips trades during disconnection.
        return None

    def accept(self, update):
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
            value = update.transaction
            slot = value.slot
            self.received += 1
            try:
                raw = normalize(value,self.store.block_time(slot))
            except (Unsupported,ValueError,IndexError) as exc:
                # A schema/metadata incompatibility is fetched once via PUBLIC
                # RPC instead of dropping the event or fabricating its contents.
                if len(value.transaction.signature) != 64:
                    raise Unsupported('stream-invalid-signature') from None
                self.added += self.store.enqueue([b58encode(value.transaction.signature)])
                self.notices.emit('stream transaction requires public RPC decoding',
                    {'slot':slot,'type':type(exc).__name__},key='grpc-normalize')
            else:
                now=time.time(); a=self.anchor
                live=bool(a and slot>=self.live_floor and a[0]<=slot<=a[0]+8
                          and 0<=now-a[2]<=2 and -2<=now-a[1]<=self.config.signal_age)
                raw['_stream']={'commitment':self.config.hotlist_commitment,'live':live}
                self.added += self.store.stream_transaction(raw)
        if slot > self.last_slot:
            # Only reached once the incoming transaction (if any) is durable.
            self.store.set_stream_state('grpc_slot',int(slot))
            self.last_slot = slot

    async def connection(self, channel, addresses, start):
        call = stream_call(channel,self.config)
        try:
            await call.write(subscription(addresses,start,commitment=self.config.hotlist_commitment))
            self.store.set_stream_state('grpc_watch',sorted(addresses))
            self.subscribed = set(addresses)
            last_update = time.monotonic()
            last_write = time.monotonic()
            # Do not cancel call.read() just to refresh filters: cancelling an
            # aio gRPC read cancels the whole stream.
            read = asyncio.create_task(call.read())
            try:
                while True:
                    done,_ = await asyncio.wait({read},timeout=1)
                    if done:
                        update = read.result()
                        if update is grpc.aio.EOF:
                            raise RuntimeError('grpc-ended')
                        last_update = time.monotonic()
                        self.connected = True
                        if update.WhichOneof('update_oneof') == 'ping':
                            await call.write(pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
                        else:
                            self.accept(update)
                        read = asyncio.create_task(call.read())
                    if time.monotonic()-last_update > 30:
                        raise RuntimeError('grpc-idle-timeout')
                    if time.monotonic()-last_write >= 2:
                        desired = self.addresses()
                        if not desired:
                            return
                        if desired != addresses:
                            await call.write(subscription(desired,commitment=self.config.hotlist_commitment))
                            addresses = desired
                            self.subscribed = set(desired)
                            self.store.set_stream_state('grpc_watch',sorted(desired))
                        last_write = time.monotonic()
            finally:
                read.cancel()
                await asyncio.gather(read,return_exceptions=True)
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
            try:
                start = await self.resume_slot(addresses)
                async with channel_for(self.config) as channel:
                    await self.connection(channel,addresses,start)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # gRPC details/debug strings can contain credentials. Emit only
                # a status enum; permission/filter rejection is never hidden.
                status = exc.code().name if isinstance(exc,grpc.RpcError) else type(exc).__name__
                self.notices.emit('hotlist Alchemy stream unavailable; retrying live subscription',
                    {'status':status,'retry_s':backoff},alert=True,key='grpc-error')
                self.reconnects += 1
            finally:
                self.connected = False
                self.subscribed.clear()
            if time.monotonic()-began > 30:
                backoff = 1
            await asyncio.sleep(backoff)
            backoff = min(60,backoff*2)
