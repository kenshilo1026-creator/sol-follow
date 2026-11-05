"""Durable full-position exits for dev sales and failed dev holding checks."""
import asyncio
import base64
import json
import time
from solders.transaction import VersionedTransaction
from chain_common.transaction import Tx
from chain_common.primitives import WSOL
from trade_execution.builder import build_sell, BuildError
from trade_execution.simulation import reusable as reusable_simulation
from features.audit.writer import record
from features.notifications.buy_failures import fee_context
from features.funding.work import WalletFlights
from features.strategy.startup_positions import retire_empty_position
from trade_execution.quote_sweep import enqueue as enqueue_sweep, recipe_for


class DevExit:
    def __init__(self,config,store,rpc,notices,priority,builder=build_sell,*,flights=None,balance_rpc=None):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.builder=builder
        self.balance_rpc=balance_rpc or rpc
        self.flights=flights or WalletFlights()
        self.wake=asyncio.Event()
        self.next={}
        self.fill_waiters={}

    def update(self,buy_id,**fields):
        if not fields or set(fields)-{'state','amount','raw','signature','last_height','min_out','reason'}:
            raise ValueError('invalid-dev-exit-update')
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL')
            c.execute('UPDATE dev_exits SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE buy_id=?',
                      (*fields.values(),time.time(),buy_id))

    def notice(self,kind,buy,**detail):
        popularity=self.store.rows('trading',"SELECT detail FROM holder_checks WHERE buy_id=? AND state='triggered'",(buy['id'],))
        if popularity and kind.startswith('dev exit '):
            kind=kind.replace('dev exit ','holder exit ',1)
            detail={**json.loads(popularity[0]['detail']),**detail}
        self.notices.emit(kind,{'mint':buy['mint'],'order':buy['id'],'mode':buy['mode'],**detail},
                          alert=True,key=kind+':'+buy['id'],interval=60)

    def discover(self):
        with self.store.db('trading') as c:
            c.execute("INSERT OR IGNORE INTO dev_exits(buy_id,state,updated) "
                "SELECT o.id,'waiting',? FROM orders o JOIN token_dev_holdings d ON d.mint=o.mint "
                "JOIN order_routes r ON r.order_id=o.id WHERE o.side='buy' AND o.mode=? AND d.state='blocked' "
                "AND o.state IN ('reserved','signed','submitted','unknown','confirmed','finalized','dry-simulated')",
                (time.time(),self.config.mode))

    def fill_observed(self,oid):
        waiter=self.fill_waiters.get(oid)
        if waiter:waiter.set()
        self.next.pop(oid,None);self.wake.set()

    def processed_fill(self,buy):
        rows=self.store.rows('trading','SELECT * FROM processed_buy_fills WHERE order_id=? AND signature=?',
                             (buy['id'],buy['signature']))
        fill=rows[0] if rows else None
        if (fill and 0<=time.time()-fill['observed']<=30 and
                not self.store.rows('funding','SELECT 1 FROM dead_slots WHERE slot=?',(fill['slot'],))):return fill
        return None

    async def buy_evidence(self,buy):
        fill=self.processed_fill(buy)
        if fill:return fill,None
        # If the buy lands while confirmed HTTP is waiting, take its streamed
        # balances immediately instead of waiting for the HTTP indexing timeout.
        waiter=asyncio.Event();self.fill_waiters[buy['id']]=waiter
        receipt=asyncio.create_task(self.rpc.transaction(buy['signature'],commitment='confirmed'))
        arrived=asyncio.create_task(waiter.wait())
        try:
            await asyncio.wait((receipt,arrived),return_when=asyncio.FIRST_COMPLETED)
            fill=self.processed_fill(buy)
            if fill:return fill,None
            return None,await receipt
        finally:
            self.fill_waiters.pop(buy['id'],None)
            for task in (receipt,arrived):
                if not task.done():task.cancel()
            await asyncio.gather(receipt,arrived,return_exceptions=True)

    def live_trigger(self,buy,job):
        if job['reason']!='dev-sell-100-percent':return True
        events=self.store.rows('trading',"SELECT slot FROM dev_sell_events WHERE buy_id=? AND state='triggered'",(buy['id'],))
        return not events or any(not self.store.rows('funding','SELECT 1 FROM dead_slots WHERE slot=?',(e['slot'],)) for e in events)

    async def step(self,buy,job,request):
        if job['signature']:
            await self.reconcile(buy,job,request)
            return
        if self.config.dry_run:
            self.update(buy['id'],state='dry-skipped',reason='dry-run-no-real-position')
            self.notice('dev exit dry run',buy,reason='dry-run-no-real-position')
            return
        if buy['state']=='failed':
            self.update(buy['id'],state='cancelled',reason='buy-failed')
            return
        if not self.live_trigger(buy,job):
            return  # Keep watching for new sale evidence after a known fork rollback.
        if not buy['signature']:return  # Build may be in flight; pre-sign gate will cancel it.
        began=time.monotonic();timings={}
        def mark(stage):timings[stage]=round((time.monotonic()-began)*1000,3)
        fill_source='finalized-position'
        simulation_source=None
        wallet=self.config.wallet_address
        signed_buy=VersionedTransaction.from_bytes(base64.b64decode(buy['raw'],validate=True))
        if str(signed_buy.message.account_keys[0])!=wallet:raise ValueError('exit-wallet-mismatch')
        position=self.store.rows('trading','SELECT amount FROM positions WHERE mint=? AND mode=?',(buy['mint'],buy['mode']))
        proof_slot=request['minSlot']
        if buy['state']=='finalized' and position:
            # Finalized fill accounting is already verified and durable. Avoid
            # another getTransaction on the urgent full-exit path.
            amount=int(position[0]['amount'])
        else:
            fill,raw=await self.buy_evidence(buy)
            if fill:
                amount=int(fill['amount']);proof_slot=max(proof_slot,fill['slot']);fill_source='grpc-processed'
            else:
                if not raw or raw.get('meta',{}).get('err') is not None:return
                tx=Tx(raw)
                if tx.signature!=buy['signature'] or wallet not in tx.signers:raise ValueError('exit-buy-proof-mismatch')
                amount=tx.owner_tokens('post',wallet,buy['mint'])-tx.owner_tokens('pre',wallet,buy['mint'])
                proof_slot=max(proof_slot,tx.slot);fill_source='confirmed-receipt'
            if amount<=0:raise ValueError('exit-buy-amount-unavailable')
            if position:amount=min(amount,int(position[0]['amount']))
        mark('fill_ready_ms')
        if amount<=0:
            self.update(buy['id'],state='cancelled',reason='position-already-exited')
            return
        if await retire_empty_position(self.config,self.store,self.balance_rpc,self.notices,buy['mint']):return
        events=self.store.rows('trading',"SELECT MAX(slot) AS slot FROM dev_sell_events WHERE buy_id=? AND state='triggered'",(buy['id'],))
        event_slot=events[0]['slot'] or 0
        request={**request,'minSlot':max(proof_slot,event_slot),'fastExit':True}
        commitment=self.config.hotlist_commitment
        self.priority.active+=1
        try:
            result=await self.builder(self.config,request,wallet,amount)
            mark('built_ms')
            wire=base64.b64decode(result['transaction'],validate=True)
            unsigned=VersionedTransaction.from_bytes(wire)
            if (len(wire)>1232 or unsigned.message.header.num_required_signatures!=1
                    or str(unsigned.message.account_keys[0])!=wallet or result.get('side')!='sell'
                    or result['wallet']!=wallet or result['mint']!=buy['mint']
                    or result['quoteMint']!=request['quoteMint'] or int(result['amount'])!=amount
                    or not 0<int(result['minOut'])<=int(result['quotedOut'])):
                raise ValueError('invalid-exit-transaction')
            if reusable_simulation(result,unsigned,slot=request['minSlot'],commitment=commitment,
                                   priority_fee_lamports=self.config.priority_fee_lamports):
                simulation_source='sdk-reused'
            else:
                simulation_source='python-fallback'
                simulation=await self.rpc.call('simulateTransaction',[result['transaction'],
                    {'encoding':'base64','sigVerify':False,'commitment':commitment,'minContextSlot':request['minSlot']}])
                if simulation['value']['err'] is not None:raise ValueError('exit-simulation-rejected')
            mark('simulated_ms')
            if not self.live_trigger(buy,job):
                return  # Unsigned intent waits for non-dead sale evidence.
            if fill_source=='grpc-processed' and self.store.rows('funding','SELECT 1 FROM dead_slots WHERE slot=?',(fill['slot'],)):
                raise ValueError('exit-buy-dead-slot')
            keypair=self.config.wallet_keypair
            if keypair is None or str(keypair.pubkey())!=wallet:raise ValueError('exit-wallet-key-required')
            signed=VersionedTransaction(unsigned.message,[keypair])
            encoded=base64.b64encode(bytes(signed)).decode();signature=str(signed.signatures[0])
            # FULL commit before any send; a crash/timeout never permits new signing.
            self.update(buy['id'],state='signed',amount=str(amount),raw=encoded,signature=signature,
                        last_height=int(result['lastHeight']),min_out=result['minOut'],reason=job['reason'] or 'dev-holdings-over-limit')
            mark('signed_ms')
            submit_started_at=time.time()
            try:
                sent=await self.rpc.call('sendTransaction',[encoded,{'encoding':'base64','skipPreflight':True,
                    'preflightCommitment':commitment,'maxRetries':0,'minContextSlot':request['minSlot']}])
                if sent!=signature:raise ValueError('exit-send-signature-mismatch')
            except Exception:
                self.update(buy['id'],state='unknown')
                self.notice('dev exit unknown',buy,signature=signature)
                return
            mark('submit_ack_ms')
            self.update(buy['id'],state='submitted')
            origins=self.store.rows('trading','SELECT dev FROM dev_sell_watches WHERE buy_id=?',(buy['id'],))
            exit_stage='holder-exit' if job['reason']=='holder-check' else 'dev-exit'
            record(self.store,exit_stage,'submitted',exit_stage+'-submitted',wallet=origins[0]['dev'] if origins else '',
                   mint=buy['mint'],signature=signature,event=buy['id']+':'+signature,
                   detail={'order':buy['id'],'timing_ms':timings,'fill_source':fill_source,
                           'simulation_source':simulation_source,'builder_timing':result.get('buildTiming'),
                           'skip_preflight':True,'submit_started_at':submit_started_at,'submit_ack_at':time.time()})
            self.notice('dev exit submitted',buy,signature=signature,amount=str(amount),quote_mint=request['quoteMint'],
                        reason=job['reason'] or 'dev-holdings-over-limit',timing_ms=timings,
                        fill_source=fill_source,simulation_source=simulation_source)
        finally:
            self.priority.active-=1

    async def reconcile(self,buy,job,request):
        signature=job['signature']
        result=await self.rpc.call('getSignatureStatuses',[[signature],{'searchTransactionHistory':True}])
        status=result['value'][0]
        if status and status.get('confirmationStatus')=='finalized':
            if status.get('err') is not None:
                self.update(buy['id'],state='waiting',raw=None,signature=None,last_height=None,reason='exit-finalized-failure')
                self.notice('dev exit retry',buy,signature=signature,reason='exit-finalized-failure')
                self.next[buy['id']]=time.monotonic()+5
                return
            raw=await self.rpc.transaction(signature,commitment='finalized')
            if not raw:return
            tx=Tx(raw);wallet=self.config.wallet_address
            if tx.signature!=signature or wallet not in tx.signers or raw['meta']['err'] is not None:
                raise ValueError('exit-fill-proof-mismatch')
            sold=tx.owner_tokens('pre',wallet,buy['mint'])-tx.owner_tokens('post',wallet,buy['mint'])
            if sold!=int(job['amount']):raise ValueError('exit-fill-amount-mismatch')
            received=tx.owner_tokens('post',wallet,request['quoteMint'])-tx.owner_tokens('pre',wallet,request['quoteMint'])
            if request['quoteMint']!=WSOL:
                if received<int(job['min_out']):raise ValueError('exit-fill-below-minimum')
            recipe=recipe_for(self.store,request)
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
                current=c.execute('SELECT state FROM dev_exits WHERE buy_id=?',(buy['id'],)).fetchone()
                if current['state']=='finalized':c.commit();return
                position=c.execute('SELECT amount FROM positions WHERE mint=? AND mode=?',(buy['mint'],buy['mode'])).fetchone()
                if position:
                    if int(position['amount'])<sold:raise ValueError('exit-exceeds-position')
                    c.execute('UPDATE positions SET amount=?,updated=? WHERE mint=? AND mode=?',
                              (str(int(position['amount'])-sold),time.time(),buy['mint'],buy['mode']))
                c.execute("UPDATE dev_exits SET state='finalized',updated=? WHERE buy_id=?",(time.time(),buy['id']))
                enqueue_sweep(c,'exit:'+buy['id'],wallet,request['quoteMint'],request['quoteProgram'],received,tx.slot,recipe)
                c.commit()
            self.notice('dev exit finalized',buy,signature=signature,amount=str(sold),quote_mint=request['quoteMint'])
        elif not status:
            height=await self.rpc.call('getBlockHeight',[{'commitment':'finalized'}])
            if height>job['last_height']:
                self.update(buy['id'],state='unknown',reason='exit-status-unknown-after-expiry')
                self.notice('dev exit unknown',buy,signature=signature,reason='exit-status-unknown-after-expiry')
                self.next[buy['id']]=time.monotonic()+30
            else:
                # Re-broadcast exactly the durable signature, never a replacement sell.
                await self.rpc.call('sendTransaction',[job['raw'],{'encoding':'base64','skipPreflight':True,
                    'preflightCommitment':self.config.hotlist_commitment,'maxRetries':0}])
                self.next[buy['id']]=time.monotonic()+5

    async def once(self):
        self.discover()
        jobs=self.store.rows('trading',"SELECT e.* FROM dev_exits e JOIN orders o ON o.id=e.buy_id "
                            "WHERE o.mode=? AND e.state NOT IN ('finalized','cancelled','dry-skipped')",(self.config.mode,))
        for job in jobs:
            if time.monotonic()<self.next.get(job['buy_id'],0):continue
            buy=self.store.order(job['buy_id'])
            request=json.loads(self.store.rows('trading','SELECT request FROM order_routes WHERE order_id=?',(buy['id'],))[0]['request'])
            try:
                async with self.flights.hold(buy['mint']):
                    if self.store.rows('trading',"SELECT 1 FROM orders WHERE mint=? AND mode=? AND side='sell' AND state IN ('reserved','signed','submitted','unknown','confirmed')",(buy['mint'],buy['mode'])):
                        continue
                    await self.step(buy,job,request)
            except asyncio.CancelledError:raise
            except Exception as exc:
                # A manual sale may land after the pre-build balance read. Only
                # explicit chain zero retires it; insufficient/frozen is not zero.
                if (isinstance(exc,BuildError) and str(exc)=='sell-balance-unavailable'
                    and await retire_empty_position(self.config,self.store,self.balance_rpc,self.notices,buy['mint'])):
                    self.next.pop(buy['id'],None)
                    continue
                self.next[buy['id']]=time.monotonic()+5
                reason=str(exc) if isinstance(exc,BuildError) else type(exc).__name__
                self.notice('dev exit deferred',buy,reason=reason,quote_mint=request['quoteMint'],
                            **fee_context(exc,self.config))

    async def run(self):
        while True:
            self.wake.clear()
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.notices.emit('dev exit worker deferred',{'type':type(exc).__name__},alert=True,key='dev-exit-worker')
            try:await asyncio.wait_for(self.wake.wait(),timeout=2)
            except asyncio.TimeoutError:pass
