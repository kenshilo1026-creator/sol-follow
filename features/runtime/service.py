"""Independent service orchestration; no imports from RH or BSC."""
import asyncio
import logging
import time
from solders.pubkey import Pubkey
import aiohttp
from chain_common.rpc import Priority, Rpc
from chain_common.background_rpc import BackgroundRpc
from chain_common.transaction import Tx, Unsupported
from chain_common.primitives import SYSTEM, WSOL
from features.notifications.buy_failures import context as buy_context, unrecognized_non_sol
from features.funding.decoder import decode as funding_decode
from features.strategy.signals import Signals
from features.funding.discovery import Discovery
from features.funding.activity_filter import qualify_activity, HistoryPending
from features.funding.processed import Processed
from trade_execution.observed_buy import check as check_observed_buy
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


class Service:
    def __init__(self,config,store=None):
        self.config=config
        self.store=store or Store(config.data)
        self.notices=Notices(config,self.store)
        self.priority=Priority()
        self.signals=Signals(self.store,config)
        self.proofs=Processed(self.store)
        self.completed=self.expired=self.received=0
        self.last_pending=0
        self.growing=0
        self.metrics=Metrics()
        self.quote_builder=CachedBuilder(config,self.store,self.notices,self.priority)

    async def qualify(self,item,rpc):
        if not Pubkey.from_string(item.wallet).is_on_curve():
            return False
        result=await rpc.call('getAccountInfo',[item.wallet,{'encoding':'base64','commitment':'confirmed','minContextSlot':item.slot}])
        row=result['value']
        if row is None:
            # A closed/empty wallet cannot be certified as an active trading owner.
            return False
        if row['owner']!=SYSTEM or row.get('executable') or row['data']!=['','base64']:
            return False
        if item.provider.startswith('cex:'):
            allowed,reason=await qualify_activity(item,rpc,self.store)
            if not allowed:
                logging.getLogger(__name__).info('CEX admission rejected wallet=%s signature=%s reason=%s',
                                                 item.wallet,item.signature,reason)
                return False
        added=self.store.admit(item,self.config.hotlist_ttl,time.time())
        if added:
            self.notices.emit('hotlist-add',item.dict(),alert=True)
            if hasattr(self,'discovery'):
                self.discovery.request_history(item.wallet)
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
            self.store.job_result(row['signature'],'done','failed-transaction')
            self.completed+=1
            return
        parse_started=time.monotonic()
        processed=raw.get('_stream',{}).get('commitment')=='processed'
        proof=self.proofs.row(row['signature']) if processed else None
        if processed and (not proof or proof['state']!='pending' or not self.proofs.usable(row['signature'],slot=raw['slot'])):
            self.store.finish_processed(row['signature'],'processed-not-live-or-invalid',raw['slot'])
            self.completed+=1
            return
        tx=Tx(raw,observed_at=proof['seen'] if proof else None)
        if tx.signature!=row['signature']:
            raise ValueError('transaction-signature-mismatch')
        if tx.time>time.time()+30:
            raise Unsupported('rpc-chain-time-ahead-of-local-clock')
        if tx.time < time.time()-self.config.backfill_age:
            self.store.job_result(row['signature'],'expired','chain-time-outside-coverage',tx.slot)
            self.expired+=1
            return
        funding_items=[] if processed else funding_decode(tx,self.config)
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
        if (self.config.hotlist_commitment!='processed' or processed) and tx.time>=max(self.signals.started,time.time()-self.config.signal_age):
            for detail in unrecognized_non_sol(tx,routes):
                if (self.store.eligible(detail['source_wallet'],tx.slot,tx.time,time.time())
                        and self.proofs.usable(tx.signature,slot=tx.slot)):
                    self.notices.emit('non-SOL buy skipped',{'mode':self.config.mode,**detail},alert=True,
                        key='unsupported-buy:'+detail['event'],interval=300)
        # Reserve before awaiting execution, so a failed first attempt cannot
        # cause another buyer in this same signature to trigger a second buy.
        pending=[]
        for route in routes:
            self.quote_builder.observe(route)
            # Confirmed recovery may refresh identities/funding, never initiate a late buy.
            if self.config.hotlist_commitment=='processed' and not processed:
                continue
            trade=route.trade
            if (self.store.eligible(trade.wallet,trade.slot,trade.time,time.time())
                    and self.proofs.usable(trade.signature,slot=trade.slot)):
                allowed,detail=await check_observed_buy(self.config,route,self.quote_builder)
                if not allowed:
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
                if time.time()-row['first_seen']>self.config.backfill_age:
                    self.store.job_result(row['signature'],'expired','qualification-window-expired')
                    self.expired+=1
                    self.notices.emit('qualification expired; possible coverage loss',{'signature':row['signature']},
                                      alert=True,key='qualification-expired',interval=60)
                else:
                    await self.process(row,rpc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason=str(exc) if isinstance(exc,(Unsupported,HistoryPending)) else type(exc).__name__
                expired=self.store.retry(row,reason,time.time(),self.config.backfill_age)
                if expired:
                    self.expired+=1
                    self.notices.emit('qualification stopped',{'signature':row['signature'],'reason':reason},alert=True,key='qualification-expired')

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
            delta=pending-self.last_pending
            self.growing=self.growing+1 if delta>0 else 0
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
            if self.growing>=3 or detail['oldest_s']>300:
                self.notices.emit('funding/transaction backlog; new signals may be late',detail,alert=True,key='backlog')
            self.last_pending=pending
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
            self.executor.recover_unsigned()
            outstanding=self.store.rows('trading',"SELECT id,signature,state FROM orders WHERE reason!=? AND state IN ('reserved','signed','submitted','unknown','confirmed')",(MARKER,))
            positions=self.store.rows('trading',"SELECT mint FROM positions WHERE amount!='0'")
            if outstanding or positions:
                self.notices.emit('legacy orders/positions require manual management; sell routes unavailable',
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
                                      self.worker(self.background_rpc,False if grpc_mode else None),self.executor.reconcile()]
                    if grpc_mode:
                        coroutines.extend([self.hotlist_feed.run(),self.worker(rpc,True),
                            self.proofs.run(self.background_rpc,self.signals,self.notices,self.config.backfill_age)])
                    for coroutine in coroutines:
                        group.create_task(coroutine)
            finally:
                await self.quote_builder.close()
