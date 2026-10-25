"""Independent live notifications and resumable address-history pagination."""
import asyncio
import json
import time
from urllib.parse import urlparse
import aiohttp
from chain_common.public_rpc import report_429, defer_429_alerts
from chain_common.availability import Availability
from chain_common.endpoint_cooldown import EndpointCooldowns


FUNDING_GAP_SECONDS = 120
FUNDING_GAP_PAGES = 3


class FeedError(RuntimeError):
    """Only fixed reason labels and numeric codes may enter diagnostics."""
    def __init__(self, reason, **codes):
        super().__init__(reason)
        self.detail={'reason':reason,**{k:v for k,v in codes.items() if type(v) is int}}


async def history_page(rpc, store, address, max_age, notices):
    """Commit one page and return whether this fixed gap still needs work."""
    now = time.time()
    max_age = min(max_age, FUNDING_GAP_SECONDS)
    state = store.cursor(address)
    pending = state.get('gaps', [])
    gap = pending[0] if pending else None
    # The previous format advanced latest on page one. Recover its original
    # completed anchor from until, keeping the saved continuation even after
    # three pages or a restart. Missing legacy bounds get one fixed window.
    until = gap.get('until') if gap else state.get('latest')
    head = gap.get('head', state.get('latest')) if gap else None
    cutoff = gap.get('cutoff', now-max_age) if gap else now-max_age
    started = gap.get('started', cutoff+max_age) if gap else now
    options = {'limit': 100, 'commitment': 'confirmed'}
    if gap:
        options['before'] = gap['before']
    if until:
        options['until'] = until
    rows = await rpc.call('getSignaturesForAddress', [address, options])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('invalid-signatures-response')
    signatures = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('signature'), str) or not row['signature']:
            raise ValueError('invalid-signatures-response')
        signatures.append(row['signature'])
    if (len(set(signatures)) != len(signatures) or
            (gap and gap['before'] in signatures)):
        raise ValueError('non-progressing-signatures-response')
    # Unknown timestamps cannot establish eligibility, but do not end paging.
    eligible = [r['signature'] for r in rows if r.get('err') is None
                and type(r.get('blockTime')) is int and cutoff <= r['blockTime'] <= started+30]
    pages = gap.get('pages', 0)+1 if gap else 1
    crossed = any(type(r.get('blockTime')) is int and r['blockTime'] < cutoff for r in rows)
    more = len(rows) == 100 and not crossed and until not in signatures
    if not gap and rows:
        head = rows[0]['signature']
    if head:
        state['observed_latest'] = head
    state['latest'] = until if more else (head or until)
    state['gaps'] = ([dict(before=rows[-1]['signature'], until=until, head=head,
                          cutoff=cutoff, started=started, pages=pages)] if more else [])
    # Jobs and continuation commit atomically. Failed RPC/DB work never advances
    # the completed anchor, and history receipts never acquire live status.
    store.save_cursor(address, state, eligible)
    return more


class Discovery:
    def __init__(self, config, store, rpc, notices, sources_only=False):
        self.config, self.store, self.rpc, self.notices = config, store, rpc, notices
        self.sources_only = sources_only
        self.changed = asyncio.Event()
        self.connected = False
        self.subscribed = set()
        self.received = self.added = 0
        self.addresses = []
        self.locks = {}
        self.market_watch = None
        self.market_event = None
        self.market_subscribed = None
        self.market_addresses = set()
        self.funding_sources = set(config.cex) | set(config.privacy_pools)
        self.ws_urls=tuple(dict.fromkeys([config.funding_ws_url,config.ws]))
        self.feed_health=Availability(notices,'feed')
        self.history_health=Availability(notices,'history')
        self.endpoint_cooldown=EndpointCooldowns(config.data / 'rpc-cooldowns')
        self.failure_message='feed unavailable; history fallback active'

    def request_history(self,address):
        if address in self.funding_sources:
            self.store.request_history(address)

    async def page(self,address):
        if address not in self.funding_sources:
            return
        lock=self.locks.setdefault(address,asyncio.Lock())
        async with lock:
            for _ in range(FUNDING_GAP_PAGES):
                more = await history_page(self.rpc,self.store,address,
                                          min(self.config.backfill_age,FUNDING_GAP_SECONDS),self.notices)
                if not more:
                    return False
                await asyncio.sleep(0)
            return True

    async def urgent_history(self):
        while True:
            rows=self.store.rows('funding','SELECT * FROM history_requests WHERE due<=? ORDER BY due LIMIT 1',
                                 (time.time(),))
            if not rows:
                await asyncio.sleep(0.5)
                continue
            row=rows[0]
            if row['address'] not in self.funding_sources:
                self.store.finish_history(row)
                await asyncio.sleep(0)
                continue
            try:
                more = await self.page(row['address'])
                self.store.finish_history(row,more=more)
                self.history_health.recovered()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.finish_history(row,retry=True)
                self.history_health.failure('event catch-up deferred; durable request retained',
                                  {'type':type(exc).__name__,'address':row['address']})

    def refresh(self):
        # CEX/source coverage is never silently replaced by hotlist subscriptions.
        self.market_addresses = self.market_watch() if self.market_watch else set()
        self.addresses = list(dict.fromkeys([*self.config.cex, *self.config.privacy_pools,
                                             *([] if self.sources_only else self.store.hotlist(time.time())),
                                             *sorted(self.market_addresses)]))

    def notification(self,body,subs):
        params=body.get('params',{})
        result=params.get('result',{})
        value=result.get('value',{})
        if not value.get('signature') or value.get('err') is not None:return
        address=next((addr for addr,sub in subs.items() if sub==params.get('subscription')),None)
        if address is None:return  # Late notification from an already removed subscription.
        self.received += 1
        if address in self.market_addresses:
            if self.market_event:
                self.market_event(address,value['signature'],result.get('context',{}).get('slot'))
            # Market-only traffic wakes the price worker directly. Never inflate
            # the durable funding/create inbox with every other trader's swap.
            if address not in self.funding_sources and (self.sources_only or
                    address not in self.store.hotlist(time.time())):return
        self.added += self.store.enqueue([value['signature']],live=True)

    async def websocket(self):
        backoff = 1
        endpoint=0
        while True:
            url=self.ws_urls[endpoint]
            now=time.monotonic()
            ready=[i for i in range(len(self.ws_urls))
                   if self.endpoint_cooldown.get(self.ws_urls[(endpoint+i)%len(self.ws_urls)])<=now]
            if not ready:
                delay=min(self.endpoint_cooldown.get(u)-now for u in self.ws_urls)
                await asyncio.sleep(max(0.05,min(60,delay)))
                continue
            endpoint=(endpoint+ready[0])%len(self.ws_urls)
            url=self.ws_urls[endpoint]
            provider='alchemy' if (urlparse(url).hostname or '').endswith('.alchemy.com') else 'configured'
            source='alchemy-websocket' if provider=='alchemy' else 'public-websocket'
            read=None
            try:
                self.refresh()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.ws_connect(url, heartbeat=20,
                                                  timeout=aiohttp.ClientWSTimeout(ws_close=10), max_msg_size=2**20) as ws:
                        sent, ids, subs, requested = {}, {}, {}, {}
                        counter = 0
                        connected_at = time.monotonic()
                        self.connected = False
                        # Legacy WSS has no replay: catch up once per connection.
                        history_requested=set(self.addresses) & self.funding_sources
                        self.store.request_histories(history_requested)
                        refreshed = 0
                        while True:
                            if time.monotonic()-refreshed>2:
                                self.refresh()
                                refreshed=time.monotonic()
                            desired = set(self.addresses)
                            for address in set(sent)-desired:
                                if address in subs:
                                    counter += 1
                                    await ws.send_json({'jsonrpc':'2.0','id':counter,'method':'logsUnsubscribe','params':[subs.pop(address)]})
                                sent.pop(address,None)
                            # Limit outstanding acknowledgements instead of bursting
                            # every address subscription at the server on reconnect.
                            for address in sorted(desired-set(sent))[:max(0,8-len(ids))]:
                                counter += 1
                                ids[counter] = address
                                requested[counter] = time.monotonic()
                                sent[address] = counter
                                await ws.send_json({'jsonrpc':'2.0','id':counter,'method':'logsSubscribe',
                                    'params':[{'mentions':[address]},{'commitment':'confirmed'}]})
                                if address not in history_requested:
                                    self.request_history(address)
                                    history_requested.add(address)
                            if any(time.monotonic()-stamp>15 for stamp in requested.values()):
                                raise FeedError('subscription-ack-timeout')
                            self.connected=bool(desired) and desired<=set(subs)
                            if backoff>1 and self.connected and time.monotonic()-connected_at>=30:
                                self.feed_health.recovered('feed recovered; history catch-up remains active',
                                    {'provider':provider})
                                backoff=1
                            # Keep one receive alive while refreshing subscriptions;
                            # idle polling must not cancel aiohttp's heartbeat read.
                            if read is None:read=asyncio.create_task(ws.receive())
                            changed=asyncio.create_task(self.changed.wait())
                            try:
                                done,_=await asyncio.wait({read,changed},timeout=1,return_when=asyncio.FIRST_COMPLETED)
                            finally:
                                changed.cancel()
                                await asyncio.gather(changed,return_exceptions=True)
                            self.changed.clear()
                            if read not in done:
                                continue
                            msg=read.result()
                            read=None
                            if msg.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):
                                raise FeedError('websocket-closed',close_code=getattr(ws,'close_code',None),
                                                frame_code=msg.data)
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            body = json.loads(msg.data)
                            if (body.get('error') or {}).get('code') in (429,'429'):
                                self.endpoint_cooldown[url]=time.monotonic()+10
                                with defer_429_alerts():report_429(source,'subscription','WS-RPC')
                            if 'id' in body:
                                addr = ids.pop(body['id'],None)
                                requested.pop(body['id'],None)
                                if addr:
                                    if 'error' in body:
                                        raise FeedError('subscription-rejected',rpc_code=body['error'].get('code'))
                                    if type(body.get('result')) is not int:
                                        raise FeedError('subscription-invalid-result')
                                    if addr not in desired:
                                        counter += 1
                                        await ws.send_json({'jsonrpc':'2.0','id':counter,'method':'logsUnsubscribe','params':[body['result']]})
                                    else:
                                        subs[addr] = body['result']
                                        if self.market_subscribed:self.market_subscribed(addr)
                                    self.subscribed = set(subs)
                            self.notification(body,subs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc,aiohttp.WSServerHandshakeError) and exc.status==429:
                    try:delay=max(1,min(120,float((exc.headers or {}).get('Retry-After','10'))))
                    except (TypeError,ValueError):delay=10
                    self.endpoint_cooldown[url]=time.monotonic()+delay
                    with defer_429_alerts():report_429(source,'connect','WS-handshake')
                detail={'type':type(exc).__name__,'retry_s':backoff,'provider':provider}
                if isinstance(exc,FeedError):detail.update(exc.detail)
                elif isinstance(exc,aiohttp.WSServerHandshakeError):detail.update(reason='websocket-handshake',http_status=exc.status)
                self.feed_health.failure(self.failure_message, detail)
                endpoint=(endpoint+1)%len(self.ws_urls)
            finally:
                if read is not None:
                    read.cancel()
                    await asyncio.gather(read,return_exceptions=True)
                self.connected = False
                self.subscribed.clear()
            await asyncio.sleep(backoff)
            backoff = min(60,backoff*2)

    async def history(self):
        # Compatibility entrypoint. No idle per-address network polling.
        await self.urgent_history()
