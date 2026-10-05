"""Independent service orchestration; no imports from RH or BSC."""
import asyncio
import time
from solders.pubkey import Pubkey
import aiohttp
from chain_common.rpc import Priority, Rpc
from chain_common.transaction import Tx, Unsupported
from chain_common.primitives import SYSTEM
from features.funding.decoder import decode as funding_decode
from features.strategy.signals import Signals
from features.funding.discovery import Discovery
from features.database.maintenance import Maintenance
from features.database.storage import Store
from launchpads import ENABLED, pump_fun, stonk
from launchpads.create import inspect as inspect_creates, SUPPORTED as CREATE_DECODERS
from share_common.notify import Notices
from share_common.metrics import Metrics
from trade_execution import VENUES
from trade_execution.route import decode as decode_buy_route
from trade_execution.executor import Executor, MARKER


class Service:
    def __init__(self,config,store=None):
        self.config=config
        self.store=store or Store(config.data)
        self.notices=Notices(config,self.store)
        self.priority=Priority()
        self.signals=Signals(self.store,config)
        self.completed=self.expired=self.received=0
        self.last_pending=0
        self.growing=0
        self.metrics=Metrics()

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
        added=self.store.admit(item,self.config.hotlist_ttl,time.time())
        if added:
            self.notices.emit('hotlist-add',item.dict(),alert=True)
            if hasattr(self,'discovery'):
                self.discovery.request_history(item.wallet)
        return added

    async def process(self,row,rpc):
        raw=await rpc.transaction(row['signature'])
        if raw is None:
            raise RuntimeError('transaction-not-indexed')
        if raw.get('meta') and raw['meta'].get('err') is not None:
            self.store.job_result(row['signature'],'done','failed-transaction')
            self.completed+=1
            return
        parse_started=time.monotonic()
        tx=Tx(raw)
        if tx.signature!=row['signature']:
            raise ValueError('transaction-signature-mismatch')
        if tx.time>time.time()+30:
            raise Unsupported('rpc-chain-time-ahead-of-local-clock')
        if tx.time < time.time()-self.config.backfill_age:
            self.store.job_result(row['signature'],'expired','chain-time-outside-coverage',tx.slot)
            self.expired+=1
            return
        funding_items=funding_decode(tx,self.config)
        creation=inspect_creates(tx)
        self.metrics.add('decode',time.monotonic()-parse_started)
        for item in funding_items:
            await self.qualify(item,rpc)
        for item in self.store.record_creates(creation.creates):
            self.notices.emit('launchpad-create',item.dict())
        if creation.rejected:
            self.notices.emit('create instruction rejected',{'signature':tx.signature,'rejected':creation.rejected},
                              key='create-rejected',interval=60)
        if self.config.remaining:
            self.signals.observe_holdings(tx)
        route=decode_buy_route(tx)
        if route:
            oid=self.signals.observe(route.trade)
            if oid:
                executor=getattr(self,'executor',None) or Executor(self.config,self.store,rpc,self.notices,self.priority)
                await executor.buy(oid,route)
        relevant_programs={pump_fun.PROGRAM,pump_fun.SWAP_PROGRAM,stonk.PROGRAM}
        if not route and not creation.creates and any(ix.get('programId') in relevant_programs for _,ix in tx.instructions()):
            self.notices.emit('unsupported buy route; no vote',{'signature':tx.signature},
                              key='unsupported-route',interval=60)
        self.store.job_result(row['signature'],'done','processed',tx.slot)
        self.completed+=1

    async def worker(self,rpc):
        turn=0
        while True:
            turn+=1
            try:
                rows=self.store.due(1,time.time(),oldest=turn%5==0)
            except Exception as exc:
                self.notices.emit('funding queue unavailable',{'type':type(exc).__name__},alert=True,key='queue-error')
                await asyncio.sleep(0.5)
                continue
            if not rows:
                await asyncio.sleep(0.2)
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
                reason=str(exc) if isinstance(exc,Unsupported) else type(exc).__name__
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
                    'timing':self.metrics.snapshot(),
                    'oldest_s':round(time.time()-rows['oldest']) if rows['oldest'] else 0}
            self.notices.emit('health',detail)
            if self.growing>=3 or detail['oldest_s']>300:
                self.notices.emit('funding/transaction backlog; new signals may be late',detail,alert=True,key='backlog')
            self.last_pending=pending
            self.completed=self.expired=discovery.received=discovery.added=0
            self.store.new_jobs=self.store.revisited_jobs=0
            self.signals.prune()

    async def run(self):
        timeout=aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            rpc=Rpc(session,self.config.rpc,self.priority)
            genesis=await rpc.call('getGenesisHash',[])
            if genesis!='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2d1':
                raise ValueError('only-solana-mainnet-beta-is-supported')
            discovery=Discovery(self.config,self.store,rpc,self.notices)
            self.discovery=discovery
            self.executor=Executor(self.config,self.store,rpc,self.notices,self.priority)
            self.executor.recover_unsigned()
            outstanding=self.store.rows('trading',"SELECT id,signature,state FROM orders WHERE reason!=? AND state IN ('reserved','signed','submitted','unknown','confirmed')",(MARKER,))
            positions=self.store.rows('trading',"SELECT mint FROM positions WHERE amount!='0'")
            if outstanding or positions:
                self.notices.emit('legacy orders/positions require manual management; sell routes unavailable',
                    {'orders':outstanding,'position_mints':[p['mint'] for p in positions]},alert=True)
            self.notices.emit('service started',{'mode':self.config.mode,'cex_sources':len(self.config.cex),
                'launchpads':ENABLED,'execution_venues':VENUES,'trading_enabled':not self.config.dry_run,
                'create_decoders':CREATE_DECODERS,
                'threshold':self.config.n,'window_s':self.config.window},alert=True)
            async with asyncio.TaskGroup() as group:
                for coroutine in [self.notices.run(),discovery.websocket(),discovery.history(),discovery.urgent_history(),self.finalized(rpc),
                                  Maintenance(self.config,self.store,self.priority,self.notices).run(),self.health(discovery,rpc),
                                  self.worker(rpc),self.executor.reconcile()]:
                    group.create_task(coroutine)
