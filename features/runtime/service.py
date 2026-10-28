"""Independent service orchestration; no imports from RH or BSC."""
import asyncio
import logging
import time
from urllib.parse import urlparse
from features.audit.writer import record, trade_record, funding_candidates, skipped_transaction
from solders.pubkey import Pubkey
import aiohttp
from chain_common.rpc import Priority, Rpc, RpcError
from chain_common.background_rpc import BackgroundRpc
from features.funding.rpc import FundingRpc
from features.funding.work import WalletFlights, TransactionReads
from features.funding.confirmation_rpc import ConfirmationRpc, ConfirmationUnavailable
from features.funding.stream_rpc import StreamHeadRpc
from chain_common.transaction import Tx, Unsupported
from chain_common.primitives import SYSTEM
from features.funding.decoder import decode as funding_decode
from features.strategy.dev_create import CreateSignals
from features.strategy.market_cap import MarketCapGate
from features.strategy.take_profit import TakeProfit
from features.strategy.dev_sell import DevSell
from features.strategy.create_dev_gate import CreateDevGate
from features.funding.discovery import Discovery, FUNDING_GAP_SECONDS
from features.funding.activity_filter import qualify_activity, HistoryPending
from features.funding.backlog import BacklogMonitor
from features.funding.processed import Processed
from features.database.maintenance import Maintenance
from features.database.storage import Store
from features.database.errors import detail as database_error
from launchpads import ENABLED, pump_fun, stonk
from launchpads.create import inspect as inspect_creates, SUPPORTED as CREATE_DECODERS
from share_common.notify import Notices
from share_common.metrics import Metrics
from trade_execution.cache import CachedBuilder
from trade_execution import VENUES
from trade_execution.route import decode_all as decode_buy_routes
from trade_execution.executor import Executor, MARKER
from trade_execution.rpc import ExecutionRpc
from trade_execution.dev_exit import DevExit
from trade_execution.quote_sweep import QuoteSweep


class Service:
    def __init__(self,config,store=None):
        self.config=config
        self.store=store or Store(config.data)
        self.notices=Notices(config,self.store)
        from chain_common.endpoint_cooldown import EndpointCooldowns
        self.priority=Priority(EndpointCooldowns(config.data / "rpc-cooldowns"))
        self.signals=CreateSignals(self.store,config)
        self.proofs=Processed(self.store)
        self.completed=self.expired=self.received=0
        self.backlog=BacklogMonitor(self.store,self.notices)
        self.metrics=Metrics()
        self.funding_flights=WalletFlights()
        self.active_jobs=set()
        self.exit_flights=WalletFlights()
        self.quote_builder=CachedBuilder(config,self.store,self.notices,self.priority)
        self.market_cap=MarketCapGate(config,self.store,self.notices,self.quote_builder)
        self.dev_holdings=CreateDevGate(config,self.store,self.notices)

    async def qualify(self,item,rpc):
        record(self.store,'funding','observed','candidate',wallet=item.wallet,signature=item.signature,
               event=item.event,detail=item.dict())
        try:
            async with self.funding_flights.hold(item.wallet):
                return await self._qualify(item,rpc)
        except Exception as exc:
            reason=str(exc) if isinstance(exc,HistoryPending) else type(exc).__name__
            record(self.store,'qualification','pending',reason,wallet=item.wallet,
                   signature=item.signature,event=item.event)
            raise

    async def _qualify(self,item,rpc):
        def decision(outcome,reason):
            record(self.store,'qualification',outcome,reason,wallet=item.wallet,
                   signature=item.signature,event=item.event,detail={'funding_slot':item.slot,'funding_time':item.time,
                   'hotlist_ttl_s':self.config.hotlist_ttl,'max_prelaunch_tx':self.config.max_prelaunch_tx})
        if not Pubkey.from_string(item.wallet).is_on_curve():
            decision('blocked','wallet-off-curve')
            return False
        result=await rpc.call('getAccountInfo',[item.wallet,{'encoding':'base64','commitment':'confirmed','minContextSlot':item.slot}])
        row=result['value']
        if row is None:
            decision('blocked','wallet-account-missing')
            # A closed/empty wallet cannot be certified as an active trading owner.
            return False
        if row['owner']!=SYSTEM or row.get('executable') or row['data']!=['','base64']:
            decision('blocked','wallet-not-system-account')
            return False
        if item.provider.startswith('cex:') or item.provider=='privacy-cash':
            allowed,reason=await qualify_activity(item,rpc,self.store,self.config.max_prelaunch_tx)
            if not allowed:
                decision('blocked',reason)
                return False
        added=self.store.admit(item,self.config.hotlist_ttl,time.time())
        if added:
            decision('admitted','qualified')
            self.notices.emit('hotlist-add',item.dict())
        else:
            removal=self.store.rows('funding','SELECT slot FROM hotlist_removals WHERE wallet=?',(item.wallet,))
            reason=('funding-expired' if item.time+self.config.hotlist_ttl<=time.time() else
                    'hotlist-consumed' if removal and item.slot<=removal[0]['slot'] else 'funding-already-recorded')
            decision('skipped',reason)
        return added

    async def process(self,row,rpc):
        process_started=time.monotonic()
        queue_wait_ms=round(max(0,time.time()-row.get('first_seen',time.time()))*1000,3)
        raw=self.store.stream_payload(row['signature'])
        if raw is None:
            raw=await rpc.transaction(row['signature'])
        elif raw.get('blockTime') is None and raw.get('_stream',{}).get('commitment')!='processed':
            stamp=self.store.block_time(raw['slot'])
            if stamp is None:
                stamp=await rpc.call('getBlockTime',[raw['slot']])
                if isinstance(stamp,int) and stamp>0:
                    self.store.block_time(raw['slot'],stamp)
            raw['blockTime']=stamp
        if raw is None:
            raise RuntimeError('transaction-not-indexed')
        if raw.get('meta') and raw['meta'].get('err') is not None:
            skipped_transaction(self.store,raw,self.config,'failed-transaction')
            self.store.job_result(row['signature'],'done','failed-transaction')
            self.completed+=1
            return
        parse_started=time.monotonic()
        processed=raw.get('_stream',{}).get('commitment')=='processed'
        proof=self.proofs.row(row['signature']) if processed else None
        if processed and not self.proofs.create_usable(row['signature'],raw['slot']):
            skipped_transaction(self.store,raw,self.config,'processed-not-live-or-invalid')
            self.store.finish_processed(row['signature'],'processed-not-live-or-invalid',raw['slot'])
            self.completed+=1
            return
        tx=Tx(raw,observed_at=proof['seen'] if proof else None)
        if tx.signature!=row['signature']:
            raise ValueError('transaction-signature-mismatch')
        if tx.time>time.time()+30:
            raise Unsupported('rpc-chain-time-ahead-of-local-clock')
        if tx.time < time.time()-self.config.backfill_age:
            skipped_transaction(self.store,raw,self.config,'chain-time-outside-coverage')
            self.store.job_result(row['signature'],'expired','chain-time-outside-coverage',tx.slot)
            self.expired+=1
            return
        funding_items=[] if processed else funding_decode(tx,self.config)
        if not processed:funding_candidates(self.store,tx,self.config,funding_items)
        creation=inspect_creates(tx)
        # Dry create alerts share the entry funding-age gate, but remain
        # independent of quantity/build gates, including provisional receipts.
        if self.config.dry_run:
            for item in creation.creates:
                if not self.store.create_alert_allowed(item,time.time(),self.config.min_funding_age_seconds):
                    continue
                if self.store.claim_create_alert(item.event):
                    detail={**item.dict(),'commitment':'processed' if processed else 'confirmed'}
                    try:
                        accepted=self.notices.emit('dry create detected',detail,alert=True)
                        if accepted is False:
                            raise RuntimeError('create-notification-queue-full')
                    except Exception:
                        self.store.release_create_alert(item.event)
                        raise
        self.metrics.add('decode',time.monotonic()-parse_started)
        funding_reads=TransactionReads(rpc,raw)
        for item in funding_items:
            await self.qualify(item,funding_reads)
        for item in creation.creates:
            self.quote_builder.observe_create(item)
        for item in ([] if processed else self.store.record_creates(creation.creates)):
            self.notices.emit('launchpad-create',item.dict())
        if creation.rejected:
            self.notices.emit('create instruction rejected',{'signature':tx.signature,'rejected':creation.rejected},
                              key='create-rejected',interval=60)
        # Trigger directly from decoded creates, even with no embedded dev buy.
        # Ordinary buys/sells never contribute votes or initiate an entry.
        pending=[]
        live=self.store.is_live(tx.signature,self.config.signal_age)
        backup=self.proofs.create_backup(tx.signature,tx.slot)
        # Preserve the original cache's verified swap hints; this is decoding/
        # prewarming only and cannot call the retired wallet-vote strategy.
        if live and (self.config.hotlist_commitment!='processed' or processed or backup):
            for observed in decode_buy_routes(tx):self.quote_builder.observe(observed)
        if self.config.hotlist_commitment!='processed' or processed or backup:
            for item in creation.creates:
                route=self.signals.prepare(item,tx,live)
                if route is not None:pending.append((route,item))
        for route,item in pending:
            if not self.dev_holdings.start_create(item,tx,route):
                trade_record(self.store,route.trade,'dev-create','blocked','dev-holdings-not-permitted')
                continue
            self.quote_builder.observe(route)
            oid=self.signals.reserve(route)
            if oid:
                trade_record(self.store,route.trade,'preparation','ready','create-prepared',
                    {'order':oid,'process_ms':round((time.monotonic()-process_started)*1000,3),
                     'queue_wait_ms':queue_wait_ms,
                     'source':raw.get('_stream',{}).get('source','grpc' if processed else 'http'),
                     'commitment':'processed' if processed else 'confirmed'})
                market=getattr(self,'market_feed',None)
                readiness=None
                if market is not None:
                    from chain_common.primitives import ata
                    required={route.trade.wallet,ata(route.trade.wallet,route.trade.mint,route.request()["tokenProgram"])}
                    readiness=asyncio.create_task(market.arm(required))
                else:
                    discovery=getattr(self,'discovery',None)
                    if discovery is not None:discovery.refresh()
                executor=getattr(self,'executor',None) or Executor(self.config,self.store,rpc,self.notices,self.priority)
                try:
                    if readiness is None:await executor.buy(oid,route)
                    else:await executor.buy(oid,route,readiness=readiness)
                finally:
                    if readiness is not None:
                        if not readiness.done():readiness.cancel()
                        await asyncio.gather(readiness,return_exceptions=True)
        if processed:
            self.store.finish_processed(row['signature'],'processed-provisional',tx.slot)
        else:
            self.store.job_result(row['signature'],'done','processed',tx.slot)
        self.completed+=1

    async def worker(self,rpc,streamed=None):
        turn=0
        while True:
            turn+=1
            # Expiry and cached results can finish without any async I/O.
            # Let feeds, health, and cancellation run even with a full backlog.
            await asyncio.sleep(0)
            try:
                # Clear before the synchronous DB read: no producer can slip
                # between the empty check and clearing a newly arrived event.
                if streamed:self.store.stream_ready.clear()
                rows=self.store.due_live_stream(time.time(),self.active_jobs) if streamed is not False else []
                if not rows:
                    rows=self.store.due(1,time.time(),oldest=turn%5==0,streamed=streamed,excluded=self.active_jobs)
            except Exception as exc:
                self.notices.emit('funding queue unavailable',database_error(exc),alert=True,key='queue-error')
                await asyncio.sleep(0.5)
                continue
            if not rows:
                if streamed:
                    try:await asyncio.wait_for(self.store.stream_ready.wait(),timeout=0.05)
                    except asyncio.TimeoutError:pass  # Existing delayed/retry jobs still become due.
                else:await asyncio.sleep(0.2)
                continue
            row=rows[0]
            self.active_jobs.add(row['signature'])
            try:
                # Drop untouched old recovery work before paying for a transaction
                # fetch. Admission retries and order/proof reconciliation retain
                # their existing lifetime and independent checks.
                max_age=self.config.backfill_age
                if not row.get('reason') and not self.store.is_live(row['signature'],self.config.signal_age):
                    max_age=min(max_age,FUNDING_GAP_SECONDS)
                if time.time()-row['first_seen']>max_age:
                    self.store.job_result(row['signature'],'expired','qualification-window-expired')
                    self.expired+=1
                    self.notices.emit('qualification expired; possible coverage loss',{'signature':row['signature']},
                                      key='qualification-expired',interval=60)
                else:
                    await self.process(row,rpc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason=str(exc) if isinstance(exc,(Unsupported,HistoryPending)) else type(exc).__name__
                expired=self.store.retry(row,reason,time.time(),self.config.backfill_age)
                if expired:
                    self.expired+=1
                    self.notices.emit('qualification stopped',{'signature':row['signature'],'reason':reason},key='qualification-expired')
            finally:
                self.active_jobs.discard(row['signature'])

    async def finalized(self,rpc):
        while True:
            rows=self.store.rows('funding','SELECT * FROM chain_checks ORDER BY time LIMIT 100')
            if rows:
                try:
                    result=await rpc.call('getSignatureStatuses',[[r['signature'] for r in rows],{'searchTransactionHistory':True}])
                    for row,status in zip(rows,result['value']):
                        if status and status.get('confirmationStatus')=='finalized':
                            if status.get('err') is not None:
                                self.store.invalidate(row['signature'])
                                self.signals.invalidate(row['signature'])
                                self.notices.emit('finalized failure; funding/create evidence invalidated; votes revoked',{'signature':row['signature']},alert=True)
                            else:
                                self.store.finalize_creates(row['signature'])
                            with self.store.db('funding') as c:
                                c.execute('DELETE FROM chain_checks WHERE signature=?',(row['signature'],))
                        elif time.time()-row['time']>300:
                            self.notices.emit('finalization unknown; evidence retained',{'signature':row['signature']},
                                              alert=True,key='finalization-unknown')
                except Exception as exc:
                    self.notices.emit('finalization check deferred',{'type':type(exc).__name__},
                                      alert=not isinstance(exc,ConfirmationUnavailable),key='finalization-error')
            await asyncio.sleep(5)

    async def health(self,discovery,rpc):
        while True:
            await asyncio.sleep(60)
            rows=self.store.rows('funding',"SELECT count(*) n,min(first_seen) oldest FROM jobs WHERE state='pending'")[0]
            pending=rows['n']
            delta=self.backlog.observe(pending,rows["oldest"],time.time())
            detail={'pending':pending,'pending_delta':delta,'completed':self.completed,'expired':self.expired,
                    'new_jobs':self.store.new_jobs,'revisited_jobs':self.store.revisited_jobs,
                    'ws_received':discovery.received,'ws_new_jobs':discovery.added,'ws_connected':discovery.connected,
                    'subscribed':len(discovery.subscribed),'rpc_calls':rpc.calls,'rpc_429':rpc.limited,
                    'timing':self.metrics.snapshot(),'quote_cache':self.quote_builder.stats,
                    'oldest_s':round(time.time()-rows['oldest']) if rows['oldest'] else 0}
            if hasattr(self,'hotlist_feed'):
                detail['hotlist_stream']=self.hotlist_feed.snapshot()
            if hasattr(self,'background_rpc'):
                detail['background_public_rpc']=self.background_rpc.snapshot()
            if hasattr(self,'funding_rpc'):
                detail['funding_check_rpc']=self.funding_rpc.snapshot()
            if hasattr(self,'confirmation_rpc'):
                detail['confirmation_rpc']=self.confirmation_rpc.snapshot()
            if hasattr(self,'execution_rpc'):
                detail['execution_rpc']=self.execution_rpc.snapshot()
                detail['execution_reads']=self.execution_reads.snapshot()
            if hasattr(self,'market_feed'):
                detail['market_feed']=self.market_feed.snapshot()
            if hasattr(self,'create_feed'):
                feed=self.create_feed
                detail['create_backup']={'connected':feed.connected,'subscribed':len(feed.subscribed),
                    'received':feed.received,'verified':feed.added,'pending':len(feed.pending)}
            if hasattr(self,'stream_head_rpc'):
                detail['stream_head_rpc']=self.stream_head_rpc.snapshot()
            detail['funding_ws_provider']=self.config.funding_ws_provider
            if hasattr(self,'funding_history_rpc'):
                detail['funding_history_rpc']={'provider':self.config.funding_history_provider,
                                              **self.funding_history_rpc.snapshot()}
            if hasattr(self,'source_history_rpc'):
                detail['source_history_rpc']=self.source_history_rpc.snapshot()
            self.notices.emit('health',detail)
            self.completed=self.expired=discovery.received=discovery.added=0
            self.store.new_jobs=self.store.revisited_jobs=0
            self.signals.prune()

    async def run(self):
        from features.audit.writer import BufferedDecisions
        self.store.reuse_connections=True
        writer=BufferedDecisions(self.store,self.priority)
        self.store.decision_writer=writer
        try:
            async with self.notices.running():
                audit_task=asyncio.create_task(writer.run())
                try:
                    await self._run()
                finally:
                    audit_task.cancel()
                    await asyncio.gather(audit_task,return_exceptions=True)
        finally:
            self.store.decision_writer=None
            try:
                writer.close()
            finally:
                self.store.close()

    async def _run(self):
        removed=self.store.prune_startup_jobs(time.time())
        logging.info('[SOL] startup queue cleanup removed=%s max_age_s=3600',removed)
        self.backlog=BacklogMonitor(self.store,self.notices)
        timeout=aiohttp.ClientTimeout(total=8)
        # Separate connection pools keep background pagination from occupying
        # execution sockets. Source-address pagination uses Alchemy when a key
        # is configured, with its own rate budget and endpoint-specific cooldown.
        async with (aiohttp.ClientSession(timeout=timeout) as session,
                    aiohttp.ClientSession() as background_session,
                    aiohttp.ClientSession(timeout=timeout) as read_session,
                    aiohttp.ClientSession(timeout=timeout) as proof_session):
            # Work queues/activity stay isolated; actual endpoint 429 deadlines are
            # shared by every HTTP lane and the SDK subprocesses.
            def execution_lane(pool, name, reads=None):
                cooldown=Priority(self.priority.cooldown)
                primary=Rpc(pool,self.config.execution_rpc_url,cooldown,
                            timeout=self.config.execution_timeout,source='alchemy-execution'
                            if (urlparse(self.config.execution_rpc_url).hostname or '').endswith('.alchemy.com') else 'execution')
                fallback=(Rpc(pool,self.config.execution_fallback_url,cooldown,
                              timeout=self.config.execution_timeout,source='alchemy-execution'
                              if (urlparse(self.config.execution_fallback_url).hostname or '').endswith('.alchemy.com') else 'execution')
                          if self.config.execution_fallback_url else None)
                if name=='execution-reads':
                    primary=BackgroundRpc(primary,cooldown,self.config.history_rps)
                    if fallback is not None:
                        fallback=BackgroundRpc(fallback,cooldown,self.config.funding_alchemy_rps)
                return ExecutionRpc(primary,fallback,notices=self.notices,lane=name,reads=reads)
            self.execution_reads=execution_lane(read_session,'execution-reads')
            rpc=self.execution_rpc=execution_lane(session,'execution',self.execution_reads)
            self.background_rpc=BackgroundRpc(Rpc(background_session,self.config.rpc,self.priority,
                timeout=self.config.public_timeout,source="background"),self.priority,self.config.history_rps)
            self.funding_history_rpc=(BackgroundRpc(Rpc(background_session,self.config.funding_history_url,self.priority,
                timeout=self.config.public_timeout,source="alchemy-history"),self.priority,self.config.funding_alchemy_rps)
                if self.config.alchemy_key else self.background_rpc)
            self.funding_rpc=FundingRpc(self.background_rpc,
                self.funding_history_rpc if self.config.alchemy_key
                and self.config.funding_history_url != self.config.rpc else None)
            self.source_history_rpc=FundingRpc(self.background_rpc,self.funding_rpc.alchemy,prefer_alchemy=True)
            proof_priority=Priority(self.priority.cooldown)
            proof_public=Rpc(proof_session,self.config.rpc,proof_priority,
                             timeout=self.config.public_timeout,source='confirmation')
            proof_alchemy=(Rpc(proof_session,self.config.funding_history_url,proof_priority,
                              timeout=self.config.public_timeout,source='alchemy-confirmation')
                           if self.funding_rpc.alchemy is not None else None)
            self.confirmation_rpc=ConfirmationRpc(proof_public,proof_alchemy)
            self.stream_head_rpc=StreamHeadRpc(proof_public,proof_alchemy)
            try:
                genesis=await rpc.call('getGenesisHash',[])
            except RpcError:
                logging.error('[SOL] startup rejected: mainnet RPC verification unavailable or wrong chain')
                raise
            # Solana sdk/src/genesis_config.rs: ClusterType::MainnetBeta.
            if genesis!='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d':
                logging.error('[SOL] startup rejected: RPC genesis hash does not match Solana mainnet-beta')
                raise ValueError('only-solana-mainnet-beta-is-supported')
            grpc_mode=self.config.feed_mode=='alchemy_grpc'
            discovery=Discovery(self.config,self.store,self.source_history_rpc,self.notices,sources_only=grpc_mode)
            self.discovery=discovery
            if grpc_mode:
                from features.funding.grpc_feed import HotlistFeed
                self.hotlist_feed=HotlistFeed(self.config,self.store,self.stream_head_rpc,self.notices,discovery)
                from features.funding.create_feed import CreateFeed
                self.create_rpc=FundingRpc(proof_public,proof_alchemy)
                self.create_feed=CreateFeed(self.config,self.store,self.create_rpc,self.notices)
            self.executor=Executor(self.config,self.store,rpc,self.notices,self.priority,builder=self.quote_builder,reconcile_rpc=self.execution_reads)
            self.dev_exit=DevExit(self.config,self.store,rpc,self.notices,self.priority,flights=self.exit_flights)
            self.take_profit=TakeProfit(self.config,self.store,rpc,self.notices,self.priority,
                                       flights=self.exit_flights,reconcile_rpc=self.execution_reads)
            self.dev_sell=DevSell(self.config,self.store,self.execution_reads,self.notices,self.dev_exit)
            from features.strategy.market_feed import MarketFeed
            def market_event(address,signature,slot):
                self.dev_sell.market_event(address,signature,slot)
                self.take_profit.market_event(address,signature,slot)
            self.market_feed=MarketFeed(self.config,self.store,self.execution_reads,self.notices,
                lambda:self.take_profit.watch_addresses() | self.dev_sell.watch_addresses(),
                market_event,self.dev_sell.subscribed)
            self.quote_sweep=QuoteSweep(self.config,self.store,rpc,self.notices,self.priority)
            self.executor.dev_exit=self.dev_exit
            self.dev_holdings.changed=self.dev_exit.wake.set
            self.dev_holdings.recover()
            self.executor.recover_unsigned()
            outstanding=self.store.rows('trading',"SELECT id,signature,state FROM orders WHERE reason!=? AND state IN ('reserved','signed','submitted','unknown','confirmed')",(MARKER,))
            positions=self.store.rows('trading',"SELECT mint FROM positions WHERE amount!='0'")
            if outstanding or positions:
                self.notices.emit('positions retained for exit management',
                    {'orders':outstanding,'position_mints':[p['mint'] for p in positions]},alert=True)
            self.notices.emit('service started',{'mode':self.config.mode,'cex_sources':len(self.config.cex),
                'launchpads':ENABLED,'execution_venues':VENUES,'trading_enabled':not self.config.dry_run,
                'create_decoders':CREATE_DECODERS,
                'hotlist_feed':self.config.feed_mode,'hotlist_commitment':self.config.hotlist_commitment,
                'http_policy':self.config.http_policy,
                'funding_history_provider':self.config.funding_history_provider,
                'funding_ws_provider':self.config.funding_ws_provider,
                'stream_head_alchemy_fallback':self.stream_head_rpc.alchemy is not None,
                'funding_workers':self.config.funding_workers if grpc_mode else 1,
                'funding_alchemy_rps':self.config.funding_alchemy_rps,
                'confirmation_alchemy_fallback':self.confirmation_rpc.alchemy is not None,
                'execution_rpc_fallback':bool(self.config.execution_fallback_url),
                'execution_rpc_isolated':True,
                'market_feed_connections':len(self.market_feed.feeds),
                'buy_trigger':self.config.buy_trigger,
                'take_profit_enabled':True,
                'take_profit_rules':len(self.take_profit.rules),
                'take_profit_poll_seconds':self.config.take_profit_poll_seconds,
                'buy_only_aged_minutes':str(self.config.buy_only_aged_minutes)},alert=True)
            try:
                async with asyncio.TaskGroup() as group:
                    coroutines=[self.quote_builder.warm(),discovery.websocket(),discovery.urgent_history(),self.finalized(self.confirmation_rpc),self.market_feed.run(),
                                      Maintenance(self.config,self.store,self.priority,self.notices).run(),self.health(discovery,rpc),
                                      self.executor.reconcile(),self.dev_exit.run(),self.dev_sell.run(),self.dev_sell.catch_up(),self.take_profit.run(),self.quote_sweep.run()]
                    coroutines.extend(self.worker(self.funding_rpc,False if grpc_mode else None)
                                      for _ in range(self.config.funding_workers if grpc_mode else 1))
                    if grpc_mode:
                        # One slow build must not hold the next fresh create behind it.
                        # Durable claims and active_jobs still exclude duplicate work.
                        coroutines.extend([self.hotlist_feed.run(),self.create_feed.run(),self.worker(rpc,True),self.worker(rpc,True),
                            self.proofs.run(self.confirmation_rpc,self.signals,self.notices,self.config.backfill_age)])
                    for coroutine in coroutines:
                        group.create_task(coroutine)
            finally:
                await self.dev_holdings.close()
                await self.quote_builder.close()
