"""A bounded active cache backed by permanent seen-mint and route records."""
import asyncio
from chain_common.public_rpc import sdk_event
from collections import OrderedDict
import json
import time
from pathlib import Path
from chain_common.primitives import WSOL
from features.database.quote_cache import SeenTokens
from trade_execution.builder import BuildError, payload_for, child_environment, result_or_error


class CachedBuilder:
    def __init__(self, config, store, notices, priority):
        self.config, self.notices, self.priority = config, notices, priority
        self.seen = SeenTokens(store)
        self.process = self.reader = None
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.pending = {}
        self.counter = 0
        self.queue = OrderedDict()
        self.recent = OrderedDict()
        self.stats = {}
        self.closed = False

    def enqueue(self, request, wallet):
        mint = request['mint']
        now = time.monotonic()
        if now - self.recent.get(mint, -1000) < 30:
            return
        self.recent.pop(mint, None)
        self.recent[mint] = now
        self.queue[mint] = (request, wallet)
        self.queue.move_to_end(mint)
        while len(self.queue) > 128:
            self.queue.popitem(last=False)
        while len(self.recent) > 512:
            self.recent.popitem(last=False)

    def observe(self, route):
        if route.quote_mint == WSOL:
            return
        request = self.seen.remember(route.request(), route.trade.wallet)
        if request.get('swapRecipe') and not self.seen.recipe(request['quoteMint']):
            self.seen.save_recipe(request['quoteMint'], request['swapRecipe'])
        self.enqueue(request, route.trade.wallet)

    def observe_create(self, item, enqueue=True):
        if item.quote_mint == WSOL:
            return
        from launchpads import stonk
        request = dict(route='sol_to_stonk_curve' if item.launchpad == stonk.NAME else 'sol_to_pump_curve',
                       mint=item.mint, quoteMint=item.quote_mint, tokenProgram=item.base_token_program,
                       quoteProgram=item.quote_token_program, pool=item.pool, minSlot=item.slot,
                       lookupTables=[], primeAccounts=[item.mint, item.quote_mint, item.pool])
        request = self.seen.remember(request, item.user)
        if enqueue:
            self.enqueue(request, item.user)

    async def import_creates(self):
        """Retain creations already known before the cache was introduced."""
        from dataclasses import fields
        from launchpads.create_types import Create
        cursor = 0
        while True:
            if self.priority.active:
                await asyncio.sleep(0.2)
                continue
            rows = self.seen.store.rows('funding',
                'SELECT rowid,detail FROM launch_creates WHERE rowid>? AND quote_mint!=? '
                "AND status!='invalid' AND mint NOT IN (SELECT mint FROM seen_non_sol) ORDER BY rowid LIMIT 100", (cursor, WSOL))
            if not rows:
                return
            for row in rows:
                detail = json.loads(row['detail'])
                item = Create(**{f.name: detail[f.name] for f in fields(Create)})
                self.observe_create(item, enqueue=False)
                cursor = row['rowid']
            await asyncio.sleep(0)

    async def start(self):
        async with self.start_lock:
            if self.closed:
                raise BuildError('route-builder-closed')
            if self.process and self.process.returncode is None:
                return
            # Finish the old reader before assigning requests to a new child.
            if self.reader:
                await self.reader
            self.process = await asyncio.create_subprocess_exec(
                'node', str(Path(__file__).parent/'sdk'/'worker.cjs'), cwd=self.config.root,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=child_environment(), limit=200000)
            self.reader = asyncio.create_task(self.read(self.process))

    async def read(self, process):
        try:
            while line := await process.stdout.readline():
                row = json.loads(line)
                if sdk_event(row):
                    continue
                if row.get('recipe'):
                    try:
                        self.seen.save_recipe(row['quoteMint'], row['recipe'])
                    except Exception:
                        self.notices.emit('quote cache persistence deferred', {}, key='cache-persist', interval=60)
                    continue
                future = self.pending.get(row.get('id'))
                if future and not future.done():
                    if row.get('stats'):
                        self.stats = row['stats']
                    future.set_result(row)
        except (ValueError, OSError):
            pass
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(BuildError('route-builder-stopped'))

    async def request(self, request, wallet, warm=False, operation="build"):
        await self.start()
        payload = payload_for(self.config, request, wallet)
        payload.update(prewarm=warm, operation=operation, limitAmount=str(self.config.max_observed_buy),
                       minimumUsdMicros=str(self.config.min_observed_buy_usd_micros),
                       maximumUsdMicros=str(self.config.max_observed_buy_usd_micros),
                       marketCapEnabled=bool(self.config.max_market_cap_usd_micros or self.config.min_observed_buy_usd_micros or self.config.max_observed_buy_usd_micros),
                       cacheGeneration=self.seen.store.stream_state("processed_cache_generation",0),cache={'ttlMs': self.config.quote_cache_ttl_ms,
                                         'maxAccounts': self.config.quote_cache_accounts,'commitment':self.config.hotlist_commitment},
                       blockhashCache={'ttlMs':self.config.blockhash_cache_ttl_ms,'refreshMs':self.config.blockhash_refresh_ms})
        if request['route'] in ('sol_to_stonk_curve','sol_to_pump_curve'):
            payload['swapRecipe'] = self.seen.recipe(request['quoteMint']) or request.get('swapRecipe')
        return await self.exchange(payload)

    async def set_priority(self, active):
        await self.exchange({"operation":"priority","active":active})

    async def exchange(self, payload):
        await self.start()
        self.counter += 1
        identifier = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            async with self.write_lock:
                payload["activeTrades"]=self.priority.active
                if payload.get("operation")=="priority":
                    payload["active"]=self.priority.active
                self.process.stdin.write((json.dumps({'id': identifier, 'input': payload})+'\n').encode())
                await self.process.stdin.drain()
            background=payload.get("prewarm") or payload.get("operation") in ("warm_limit","bootstrap")
            if background:
                # Pausing background work is not a hung foreground build.
                row=await future
            else:
                row = await asyncio.wait_for(future, timeout=30)
            result_or_error(row)
            return row['result']
        except asyncio.TimeoutError as exc:
            # Abandon the whole worker so a hung RPC cannot accumulate detached
            # builds; other pending calls fail safely and the next call restarts.
            if self.process and self.process.returncode is None:
                self.process.kill()
            if self.reader:
                await self.reader
            raise BuildError('route-builder-timeout') from exc
        finally:
            self.pending.pop(identifier, None)

    async def market_cap(self,route):
        return await self.request(route.request(),self.config.wallet_address or route.trade.wallet,operation='market_cap')

    async def quote_minimum(self, route):
        return await self.request(route.request(),self.config.wallet_address or route.trade.wallet,operation='quote_minimum')

    async def quote_limit(self, route):
        return await self.request(route.request(),self.config.wallet_address or route.trade.wallet,operation="quote_limit")

    async def __call__(self, config, route, wallet):
        return await self.request(route.request(), wallet)

    async def warm(self):
        # Initialise the shared blockhash before any target is known.
        try:
            await self.request({"route":"prime","minSlot":0,"lookupTables":[],"primeAccounts":[]},
                               self.config.wallet_address,operation="bootstrap")
        except BuildError:
            self.notices.emit("blockhash warm deferred",{},key="blockhash-warm",interval=60)
        await self.import_creates()
        for request, wallet in self.seen.recent():
            if request['route']=='prime':
                request={**request,'route':'sol_to_pump_curve'}
                request=self.seen.remember(request,wallet)
            self.enqueue(request, wallet)
        # Start/load SDKs before the first qualifying signal.
        await self.start()
        while True:
            if self.priority.active or not self.queue:
                await asyncio.sleep(0.2)
                continue
            _, (request, wallet) = self.queue.popitem(last=False)
            try:
                await self.request(request, self.config.wallet_address or wallet, warm=True)
                if request["route"]!="prime":
                    await self.request(request,self.config.wallet_address or wallet,operation="warm_limit")
            except Exception as exc:
                reason = str(exc) if isinstance(exc, BuildError) else type(exc).__name__
                self.notices.emit('quote cache warm deferred', {'mint': request['mint'], 'reason': reason},
                                  key='cache-warm', interval=60)
            await asyncio.sleep(1)

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.kill()
        if self.reader:
            await self.reader
