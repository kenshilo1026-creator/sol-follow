"""Independent service orchestration; no imports from RH or BSC."""
import asyncio
import logging
import time
from features.audit.writer import record, trade_record, funding_candidates, skipped_transaction
from solders.pubkey import Pubkey
import aiohttp
from chain_common.rpc import Priority, Rpc
from chain_common.background_rpc import BackgroundRpc
from chain_common.transaction import Tx, Unsupported
from chain_common.primitives import SYSTEM, WSOL
from features.notifications.buy_failures import context as buy_context, unrecognized_non_sol
from features.funding.decoder import decode as funding_decode
from features.strategy.signals import Signals
from features.strategy.market_cap import MarketCapGate
from features.strategy.dev_holdings import DevHoldingsGate
from features.funding.discovery import Discovery, FUNDING_GAP_SECONDS
from features.funding.activity_filter import qualify_activity, HistoryPending
from features.funding.backlog import BacklogMonitor
from features.funding.processed import Processed
from trade_execution.observed_buy import check as check_observed_buy, check_minimum, check_ignore
from features.database.maintenance import Maintenance
from features.database.storage import Store
from launchpads import ENABLED, pump_fun, stonk
from launchpads.create import inspect as inspect_creates, SUPPORTED as CREATE_DECODERS
from share_common.notify import Notices
from share_common.metrics import Metrics
from trade_execution.cache import CachedBuilder
from trade_execution import VENUES
from trade_execution.route import decode_all as decode_buy_routes
from trade_execution.executor import Executor, MARKER
from trade_execution.dev_exit import DevExit
from trade_execution.quote_sweep import QuoteSweep


class Service:
    def __init__(self,config,store=None):
        self.config=config
        self.store=store or Store(config.data)
        self.notices=Notices(config,self.store)
        self.priority=Priority()
        self.signals=Signals(self.store,config)
        self.proofs=Processed(self.store)
        self.completed=self.expired=self.received=0
        self.backlog=BacklogMonitor(self.store,self.notices)
        self.metrics=Metrics()
        self.quote_builder=CachedBuilder(config,self.store,self.notices,self.priority)
        self.market_cap=MarketCapGate(config,self.store,self.notices,self.quote_builder)
        self.dev_holdings=DevHoldingsGate(config,self.store,self.notices)

    async def qualify(self,item,rpc):
        record(self.store,'funding','observed','candidate',wallet=item.wallet,signature=item.signature,
               event=item.event,detail=item.dict())
        try:
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
                   'hotlist_ttl_s':self.config.hotlist_ttl})
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
        if item.provider.startswith('cex:'):
            allowed,reason=await qualify_activity(item,rpc,self.store)
            if not allowed:
                decision('blocked',reason)
                logging.getLogger(__name__).info('CEX admission rejected wallet=%s signature=%s reason=%s',
                                                 item.wallet,item.signature,reason)
                return False
        added=self.store.admit(item,self.config.hotlist_ttl,time.time())
        if added:
            decision('admitted','qualified')
            self.notices.emit('hotlist-add',item.dict(),alert=True)
        else:
            removal=self.store.rows('funding','SELECT slot FROM hotlist_removals WHERE wallet=?',(item.wallet,))
            reason=('funding-expired' if item.time+self.config.hotlist_ttl<=time.time() else
                    'hotlist-consumed' if removal and item.slot<=removal[0]['slot'] else 'funding-already-recorded')
            decision('skipped',reason)
        return added

    async def process(self,row,rpc):
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
        if processed and (not proof or proof['state']!='pending' or not self.proofs.usable(row['signature'],slot=raw['slot'])):
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
        self.metrics.add('decode',time.monotonic()-parse_started)
        for item in funding_items:
            await self.qualify(item,rpc)
        for item in creation.creates:
            self.quote_builder.observe_create(item)
        for item in ([] if processed else self.store.record_creates(creation.creates)):
            self.notices.emit('launchpad-create',item.dict())
        if creation.rejected:
            self.notices.emit('create instruction rejected',{'signature':tx.signature,'rejected':creation.rejected},
                              key='create-rejected',interval=60)
        if self.config.remaining:
            self.signals.observe_holdings(tx)
        routes=decode_buy_routes(tx)
        live=self.store.is_live(tx.signature,self.config.signal_age)
        if live and (self.config.hotlist_commitment!='processed' or processed) and tx.time>=max(self.signals.started,time.time()-self.config.signal_age):
            for detail in unrecognized_non_sol(tx,routes):
                if (self.store.eligible(detail['source_wallet'],tx.slot,tx.time,time.time())
                        and self.proofs.usable(tx.signature,slot=tx.slot)):
                    record(self.store,'buy','blocked','unsupported-non-sol-route',wallet=detail['source_wallet'],
                           mint=detail.get('mint',''),signature=tx.signature,event=detail['event'],detail=detail)
                    self.notices.emit('non-SOL buy skipped',{'mode':self.config.mode,**detail},alert=True,
                        key='unsupported-buy:'+detail['event'],interval=300)
        # Reserve before awaiting execution, so a failed first attempt cannot
        # cause another buyer in this same signature to trigger a second buy.
        pending=[]
        for route in routes:
            # Confirmed recovery may refresh identities/funding, never initiate a late buy.
            if not live or (self.config.hotlist_commitment=='processed' and not processed):
                trade_record(self.store,route.trade,'signal','skipped','recovery-not-live')
                continue
            self.quote_builder.observe(route)
            trade=route.trade
            if (self.store.eligible(trade.wallet,trade.slot,trade.time,time.time())
                    and self.proofs.usable(trade.signature,slot=trade.slot)):
                fresh_buy=trade.side=='buy' and trade.time>=max(self.signals.started,time.time()-self.config.signal_age)
                if fresh_buy:
                    if not self.dev_holdings.start(route,rpc):
                        self.signals.reject_mint(trade.mint)
                        trade_record(self.store,trade,'dev-holdings','blocked','dev-holdings-not-permitted',
                                     self.dev_holdings.row(trade.mint))
                        continue
                    ignore_allowed,ignore_detail=await check_ignore(self.config,route,self.quote_builder)
                    if not ignore_allowed:
                        # Tiny executed payments keep their subscription, even with a loose input budget.
                        # Missing valuations also keep the wallet: they cannot prove either band.
                        ignored=ignore_detail['reason']=='source-buy-below-ignore'
                        self.store.ignore_vote(trade)
                        trade_record(self.store,trade,'ignore','ignored' if ignored else 'unavailable',
                                     ignore_detail['reason'],{**ignore_detail,'hotlist_removed':False})
                        if self.config.max_market_cap_usd_micros and not self.market_cap.row(trade.mint):
                            self.signals.reject_mint(trade.mint)
                        await self.market_cap.check(route)
                        self.notices.emit('target buy skipped',{'signature':trade.signature,
                            **buy_context(self.config,route),'stage':'target-ignore-check',
                            **ignore_detail,'hotlist_removed':False},alert=route.quote_mint!=WSOL,
                            key='target-ignore-skip:'+trade.event,interval=300)
                        continue
                    minimum_allowed,minimum_detail=await check_minimum(self.config,route,self.quote_builder)
                    if not minimum_allowed and minimum_detail['reason']=='source-buy-below-minimum':
                        self.store.remove_hotlist(trade,'buy-below-minimum')
                        self.signals.reject(trade)
                        trade_record(self.store,trade,'minimum','blocked',minimum_detail['reason'],
                                     {**minimum_detail,'hotlist_removed':True})
                        self.notices.emit('target buy skipped',{'signature':trade.signature,
                            **buy_context(self.config,route),'stage':'target-minimum-check',
                            **minimum_detail,'hotlist_removed':True},alert=route.quote_mint!=WSOL,
                            key='target-min-skip:'+trade.event,interval=300)
                        if self.config.max_market_cap_usd_micros and not self.market_cap.row(trade.mint):
                            self.signals.reject_mint(trade.mint)
                        await self.market_cap.check(route)
                        continue
                allowed,detail=await check_observed_buy(self.config,route,self.quote_builder)
                over_limit=not allowed and detail['reason'] in ('source-sol-budget-over-limit','source-token-budget-over-usd-limit')
                if over_limit and fresh_buy:
                    # Persist removal before any slower market-cap lookup.
                    self.store.remove_hotlist(trade,'buy-above-maximum')
                    self.signals.reject(trade)
                    trade_record(self.store,trade,'maximum','blocked',detail['reason'],{**detail,'hotlist_removed':True})
                    self.notices.emit('target buy skipped',{'signature':trade.signature,**buy_context(self.config,route),
                        'stage':'target-amount-check',**detail,'hotlist_removed':True},alert=route.quote_mint!=WSOL,
                        key='target-skip:'+trade.event,interval=300)
                    if self.config.max_market_cap_usd_micros and not self.market_cap.row(trade.mint):
                        self.signals.reject_mint(trade.mint)
                    await self.market_cap.check(route)  # Preserve first-observation valuation.
                    continue
                if fresh_buy:
                    if self.config.max_market_cap_usd_micros and not self.market_cap.row(trade.mint):
                        self.signals.reject_mint(trade.mint)
                    cap_allowed=await self.market_cap.check(route)
                    if not minimum_allowed:
                        trade_record(self.store,trade,'minimum','unavailable',minimum_detail['reason'],
                                     {**minimum_detail,'hotlist_removed':False})
                        self.signals.reject(trade)
                        self.notices.emit('target buy skipped',{'signature':trade.signature,
                            **buy_context(self.config,route),'stage':'target-minimum-check',
                            **minimum_detail,'hotlist_removed':False},alert=route.quote_mint!=WSOL,
                            key='target-min-skip:'+trade.event,interval=300)
                        continue
                    if not cap_allowed:
                        trade_record(self.store,trade,'market-cap','blocked','market-cap-not-permitted',
                                     self.market_cap.row(trade.mint))
                        self.signals.reject_mint(trade.mint)
                        self.signals.reject(trade)
                        continue
                if not allowed:
                    trade_record(self.store,trade,'maximum','blocked' if over_limit else 'unavailable',detail['reason'],detail)
                    self.signals.reject(trade)
                    self.notices.emit('target buy skipped',{'signature':trade.signature,**buy_context(self.config,route),
                        'stage':'target-amount-check',**detail},alert=route.quote_mint!=WSOL,
                        key='target-skip:'+trade.event,interval=300)
                    continue
            oid=self.signals.observe(trade)
            if oid:
                pending.append((oid,route))
        for oid,route in pending:
            executor=getattr(self,'executor',None) or Executor(self.config,self.store,rpc,self.notices,self.priority)
            await executor.buy(oid,route)
        relevant_programs={pump_fun.PROGRAM,pump_fun.SWAP_PROGRAM,stonk.PROGRAM}
        if not routes and not creation.creates and any(ix.get('programId') in relevant_programs for _,ix in tx.instructions()):
            for wallet in tx.signers:
                if self.store.eligible(wallet,tx.slot,tx.time,time.time()):
                    record(self.store,'buy','unavailable','unsupported-route-or-not-a-buy',
                           wallet=wallet,signature=tx.signature)
            self.notices.emit('unsupported buy route; no vote',{'signature':tx.signature},
                              key='unsupported-route',interval=60)
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
                rows=self.store.due(1,time.time(),oldest=turn%5==0,streamed=streamed)
            except Exception as exc:
                self.notices.emit('funding queue unavailable',{'type':type(exc).__name__},alert=True,key='queue-error')
                await asyncio.sleep(0.5)
                continue
            if not rows:
                await asyncio.sleep(0.05 if streamed else 0.2)
                continue
            row=rows[0]
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
                    self.notices.emit('finalization check deferred',{'type':type(exc).__name__},alert=True,key='finalization-error')
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
                feed=self.hotlist_feed
                detail['hotlist_stream']={'connected':feed.connected,'addresses':len(feed.subscribed),
                    'received':feed.received,'new_jobs':feed.added,'protobuf_bytes':feed.bytes_received,
                    'last_slot':feed.last_slot,'reconnects':feed.reconnects}
            if hasattr(self,'background_rpc'):
                detail['background_public_rpc']=self.background_rpc.snapshot()
            self.notices.emit('health',detail)
            self.completed=self.expired=discovery.received=discovery.added=0
            self.store.new_jobs=self.store.revisited_jobs=0
            self.signals.prune()

    async def run(self):
        async with self.notices.running():
            await self._run()

    async def _run(self):
        timeout=aiohttp.ClientTimeout(total=8)
        # Separate connection pools keep background pagination from occupying
        # execution sockets. Both clients still share the PUBLIC endpoint and
        # cooldown; there is deliberately no Alchemy HTTP fallback.
        async with aiohttp.ClientSession(timeout=timeout) as session, aiohttp.ClientSession() as background_session:
            rpc=Rpc(session,self.config.rpc,self.priority)
            self.background_rpc=BackgroundRpc(Rpc(background_session,self.config.rpc,self.priority,
                timeout=self.config.public_timeout,source="background"),self.priority,self.config.history_rps)
            genesis=await rpc.call('getGenesisHash',[])
            # Solana sdk/src/genesis_config.rs: ClusterType::MainnetBeta.
            if genesis!='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d':
                logging.error('[SOL] startup rejected: RPC genesis hash does not match Solana mainnet-beta')
                raise ValueError('only-solana-mainnet-beta-is-supported')
            grpc_mode=self.config.feed_mode=='alchemy_grpc'
            discovery=Discovery(self.config,self.store,self.background_rpc,self.notices,sources_only=grpc_mode)
            self.discovery=discovery
            if grpc_mode:
                from features.funding.grpc_feed import HotlistFeed
                self.hotlist_feed=HotlistFeed(self.config,self.store,rpc,self.notices,discovery)
            self.executor=Executor(self.config,self.store,rpc,self.notices,self.priority,builder=self.quote_builder,reconcile_rpc=self.background_rpc)
            self.dev_exit=DevExit(self.config,self.store,rpc,self.notices,self.priority)
            self.quote_sweep=QuoteSweep(self.config,self.store,rpc,self.notices,self.priority)
            self.executor.dev_exit=self.dev_exit
            self.dev_holdings.changed=self.dev_exit.wake.set
            self.dev_holdings.recover()
            self.executor.recover_unsigned()
            outstanding=self.store.rows('trading',"SELECT id,signature,state FROM orders WHERE reason!=? AND state IN ('reserved','signed','submitted','unknown','confirmed')",(MARKER,))
            positions=self.store.rows('trading',"SELECT mint FROM positions WHERE amount!='0'")
            if outstanding or positions:
                self.notices.emit('positions retained; only dev-limit emergency curve exits are automated',
                    {'orders':outstanding,'position_mints':[p['mint'] for p in positions]},alert=True)
            self.notices.emit('service started',{'mode':self.config.mode,'cex_sources':len(self.config.cex),
                'launchpads':ENABLED,'execution_venues':VENUES,'trading_enabled':not self.config.dry_run,
                'create_decoders':CREATE_DECODERS,
                'hotlist_feed':self.config.feed_mode,'hotlist_commitment':self.config.hotlist_commitment,
                'max_target_buy_sol':self.config.max_observed_buy/1_000_000_000,'http_policy':'configured-public-only',
                'threshold':self.config.n,'window_s':self.config.window},alert=True)
            try:
                async with asyncio.TaskGroup() as group:
                    coroutines=[self.quote_builder.warm(),discovery.websocket(),discovery.urgent_history(),self.finalized(self.background_rpc),
                                      Maintenance(self.config,self.store,self.priority,self.notices).run(),self.health(discovery,rpc),
                                      self.worker(self.background_rpc,False if grpc_mode else None),self.executor.reconcile(),self.dev_exit.run(),self.quote_sweep.run()]
                    if grpc_mode:
                        coroutines.extend([self.hotlist_feed.run(),self.worker(rpc,True),
                            self.proofs.run(self.background_rpc,self.signals,self.notices,self.config.backfill_age)])
                    for coroutine in coroutines:
                        group.create_task(coroutine)
            finally:
                await self.dev_holdings.close()
                await self.quote_builder.close()
