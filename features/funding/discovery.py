"""Independent live notifications and resumable address-history pagination."""
import asyncio
import json
import time
import aiohttp
from chain_common.public_rpc import report_429


async def history_page(rpc, store, address, max_age, notices):
    """Latest page every visit; older unfinished pages receive a second request.

    Cursor advances atomically with the durable signatures. An incomplete gap
    keeps its original boundary while new traffic continues to be discovered.
    """
    state = store.cursor(address)
    options = {'limit':100, 'commitment':'confirmed'}
    if state.get('latest'):
        options['until'] = state['latest']
    newest = await rpc.call('getSignaturesForAddress', [address, options])
    if not isinstance(newest, list):
        raise ValueError('invalid-signatures-response')
    cutoff = time.time()-max_age
    def eligible(rows):
        return [r['signature'] for r in rows if r.get('err') is None and
                (r.get('blockTime') is None or r['blockTime'] >= cutoff)]
    pending = list(state.get('gaps', []))
    if len(newest) == 100 and (newest[-1].get('blockTime') or time.time()) >= cutoff:
        pending.append({'before':newest[-1]['signature'], 'until':state.get('latest'), 'revisit':not state.get('latest')})
    if newest:
        state['latest'] = newest[0]['signature']
    state['gaps'] = pending
    store.save_cursor(address, state, eligible(newest), revisit=not options.get('until'))
    if not pending:
        return
    gap = pending[0]
    opts = {'limit':100,'commitment':'confirmed','before':gap['before']}
    if gap.get('until'):
        opts['until'] = gap['until']
    older = await rpc.call('getSignaturesForAddress', [address, opts])
    if not isinstance(older, list):
        raise ValueError('invalid-signatures-response')
    crossed = any(r.get('blockTime') is not None and r['blockTime'] < cutoff for r in older)
    if crossed:
        notices.emit('history coverage cutoff', {'address':address,'seconds':max_age}, alert=True,
                     key='history-gap:'+address, interval=1800)
    if len(older)<100 or crossed:
        pending.pop(0)
    else:
        gap['before'] = older[-1]['signature']
    state['gaps'] = pending
    store.save_cursor(address, state, eligible(older), revisit=gap.get('revisit',False))


class Discovery:
    def __init__(self, config, store, rpc, notices, sources_only=False):
        self.config, self.store, self.rpc, self.notices = config, store, rpc, notices
        self.sources_only = sources_only
        self.connected = False
        self.subscribed = set()
        self.received = self.added = 0
        self.addresses = []
        self.locks = {}

    def request_history(self,address):
        self.store.request_history(address)

    async def page(self,address):
        lock=self.locks.setdefault(address,asyncio.Lock())
        async with lock:
            await history_page(self.rpc,self.store,address,self.config.backfill_age,self.notices)

    async def urgent_history(self):
        while True:
            rows=self.store.rows('funding','SELECT * FROM history_requests WHERE due<=? ORDER BY due LIMIT 1',
                                 (time.time(),))
            if not rows:
                await asyncio.sleep(0.5)
                continue
            row=rows[0]
            try:
                await self.page(row['address'])
                self.store.finish_history(row,retry=bool(self.store.cursor(row['address']).get('gaps')))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.finish_history(row,retry=True)
                self.notices.emit('event catch-up deferred; durable request retained',
                                  {'type':type(exc).__name__,'address':row['address']},alert=True,key='history-defer')

    def refresh(self):
        # CEX/source coverage is never silently replaced by hotlist subscriptions.
        self.addresses = list(dict.fromkeys([*self.config.cex, *self.config.privacy_pools,
                                             *([] if self.sources_only else self.store.hotlist(time.time()))]))

    async def websocket(self):
        backoff = 1
        while True:
            try:
                self.refresh()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.ws_connect(self.config.ws, heartbeat=20,
                                                  timeout=aiohttp.ClientWSTimeout(ws_close=10), max_msg_size=2**20) as ws:
                        sent, ids, subs = {}, {}, {}
                        counter = 0
                        connected_at = time.monotonic()
                        self.connected = True
                        # Legacy WSS has no replay: catch up once per connection.
                        history_requested=set(self.addresses)
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
                            for address in desired-set(sent):
                                counter += 1
                                ids[counter] = address
                                sent[address] = counter
                                await ws.send_json({'jsonrpc':'2.0','id':counter,'method':'logsSubscribe',
                                    'params':[{'mentions':[address]},{'commitment':'confirmed'}]})
                                if address not in history_requested:
                                    self.request_history(address)
                                    history_requested.add(address)
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=1)
                            except asyncio.TimeoutError:
                                continue
                            if msg.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):
                                raise RuntimeError('websocket-closed')
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            body = json.loads(msg.data)
                            if (body.get('error') or {}).get('code') in (429,'429'):
                                report_429('public-websocket','subscription','WS-RPC')
                            if 'id' in body:
                                addr = ids.pop(body['id'],None)
                                if addr:
                                    if 'error' in body:
                                        raise RuntimeError('subscription-rejected')
                                    if addr not in desired:
                                        counter += 1
                                        await ws.send_json({'jsonrpc':'2.0','id':counter,'method':'logsUnsubscribe','params':[body['result']]})
                                    else:
                                        subs[addr] = body['result']
                                    self.subscribed = set(subs)
                            value = body.get('params',{}).get('result',{}).get('value',{})
                            if value.get('signature') and value.get('err') is None:
                                self.received += 1
                                self.added += self.store.enqueue([value['signature']])
                            if backoff>1 and time.monotonic()-connected_at>=30:
                                self.notices.emit('feed recovered; history catch-up remains active', {}, alert=True)
                                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc,aiohttp.WSServerHandshakeError) and exc.status==429:
                    report_429('public-websocket','connect','WS-handshake')
                self.notices.emit('feed unavailable; history fallback active', {'type':type(exc).__name__, 'retry_s':backoff},
                                  alert=True,key='feed-error')
            finally:
                self.connected = False
                self.subscribed.clear()
            await asyncio.sleep(backoff)
            backoff = min(60,backoff*2)

    async def history(self):
        # Compatibility entrypoint. No idle per-address network polling.
        await self.urgent_history()
