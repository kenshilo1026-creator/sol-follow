"""Independent public/Alchemy subscriptions for exits, with durable dedup downstream."""
import asyncio
import time
from collections import Counter
from dataclasses import replace

from chain_common.public_rpc import defer_429_alerts
from features.funding.discovery import Discovery


class LocalNotices:
    def __init__(self, notices):
        self.notices = notices

    def emit(self, kind, detail, **options):
        options['alert'] = False
        options['key'] = 'market-local:'+options.get('key', kind)
        return self.notices.emit(kind, {**detail, 'lane': 'market'}, **options)


class MarketFeed:
    def __init__(self, config, store, rpc, notices, watch, event, subscribed):
        self.notices, self.watch = notices, watch
        self.feeds = []
        self.coverage_changed = asyncio.Event()
        self.urgent = Counter()
        self.since = None
        self.notified = False
        for url in config.market_ws_urls:
            # No CEX/hotlist subscriptions or funding pagination on these sockets.
            cfg = replace(config, cex={}, privacy_pools=(), funding_ws=url)
            feed = Discovery(cfg, store, rpc, LocalNotices(notices), sources_only=True)
            feed.ws_urls=(url,)  # Two simultaneous connections, no rotation onto the same provider.
            feed.market_watch, feed.market_event, feed.market_subscribed = watch, event, subscribed
            feed.subscription_changed = self.coverage_changed.set
            self.feeds.append(feed)

    def covered(self):
        # subscribed contains only server-ACKed addresses on the current socket
        # and is cleared on disconnect. connected means ALL desired addresses
        # are ready: an unrelated pending subscription must not block this buy.
        return set().union(*(f.subscribed for f in self.feeds))

    def arm(self, required, timeout=2):
        # Like RH's local watch registration, install consumers before the
        # caller schedules its build. Only waiting for ACKs is asynchronous.
        # ACKs call DevSell.subscribed again to close the pre-subscription gap.
        required=frozenset(required)
        # Install local consumers once, rather than scanning all positions for
        # each provider. A cached ACK can only cover a registered consumer.
        watched=set(self.watch())
        self.urgent.update(required)
        for feed in self.feeds:
            feed.subscription_priority=set(self.urgent)
            feed.refresh(market_addresses=watched)
            feed.changed.set()
        released=False
        def release():
            nonlocal released
            if released:return
            released=True
            self.urgent.subtract(required)
            self.urgent=+self.urgent
            for feed in self.feeds:
                feed.subscription_priority=set(self.urgent)
                feed.changed.set()
        # Even a task cancelled before its coroutine starts releases priority.
        timer=asyncio.get_running_loop().call_later(timeout,release)
        return self.wait_armed(required,time.monotonic()+timeout,release,timer)

    async def wait_armed(self, required, deadline, release, timer):
        try:
            return await self.wait_ready(required,deadline)
        finally:
            timer.cancel()
            release()

    async def wait_ready(self, required, deadline):
        while True:
            # Clear before checking: ACK/disconnect cannot interleave with this
            # synchronous check, and any later change wakes all active buyers.
            self.coverage_changed.clear()
            if required <= self.covered():return True
            remaining=deadline-time.monotonic()
            if remaining<=0:return False
            try:
                await asyncio.wait_for(self.coverage_changed.wait(),timeout=remaining)
            except asyncio.TimeoutError:return False

    def snapshot(self):
        watched = set(self.watch())
        covered = self.covered()
        return {'connections': len(self.feeds), 'connected': sum(f.connected for f in self.feeds),
                'watched': len(watched), 'uncovered': len(watched-covered)}

    def observe(self):
        state = self.snapshot()
        if not state['uncovered']:
            if self.notified:
                self.notices.emit('market feed recovered', state, alert=True, key='market-recovered')
            self.since, self.notified = None, False
        else:
            now = time.monotonic()
            if self.since is None:
                self.since = now
            if now-self.since > 60 and not self.notified:
                self.notices.emit('market feed unavailable', state, alert=True, key='market-unavailable')
                self.notified = True

    async def monitor(self):
        while True:
            self.observe()
            await asyncio.sleep(1)

    async def run(self):
        # Per-socket 429s are local; the combined coverage monitor owns alerts.
        with defer_429_alerts():
            async with asyncio.TaskGroup() as group:
                for feed in self.feeds:
                    group.create_task(feed.websocket())
                group.create_task(self.monitor())
