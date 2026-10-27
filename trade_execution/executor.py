"""Simulate, durably sign, submit once, then reconcile by signature."""
import asyncio
import base64
import json
import time
from features.audit.writer import trade_record
from solders.transaction import VersionedTransaction
from chain_common.transaction import Tx
from trade_execution.builder import build
from trade_execution.simulation import reusable as reusable_simulation
from features.funding.processed import Processed
from features.strategy.dev_holdings import permitted as dev_permitted
from features.strategy.create_dev_gate import permitted as create_permitted
from trade_execution.native import decode_native
from features.notifications.buy_failures import context as buy_context, reason as failure_reason, fee_context
from features.notifications.buy_success import context as buy_success_context

MARKER = 'atomic-pump-buy-v1'


class Executor:
    def __init__(self, config, store, rpc, notices, priority, builder=build, reconcile_rpc=None):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.builder=builder
        self.reconcile_rpc=reconcile_rpc or rpc

    def rejection(self, route, oid=None):
        allowed=(create_permitted(self.store,route,self.config.max_dev_holding_tokens) if route.trade.side=='create'
                 else dev_permitted(self.store,route.trade.mint,self.config.max_dev_holding_tokens))
        if not allowed:return 'dev-entry-not-permitted'
        proofs=Processed(self.store)
        processed=self.config.hotlist_commitment=='processed'
        trigger=proofs.row(route.trade.signature) if processed else None
        if processed:
            if route.trade.side=='create':
                # Confirmation upgrades live evidence; it must not cancel an
                # already prepared processed create while the monitor arms.
                if not proofs.create_usable(route.trade.signature,route.trade.slot):
                    return 'source-proof-unusable'
            elif not proofs.create_backup(route.trade.signature,route.trade.slot) and (not trigger or trigger['state']!='pending'):
                return 'source-proof-not-pending'
        age=time.time()-route.trade.time
        if age<0:return 'signal-time-in-future'
        if age>self.config.signal_age:return 'signal-expired'
        if oid is not None and not proofs.order_usable(oid,require_processed=processed):
            return 'order-source-unusable'
        return None

    def fresh(self, route, oid=None):
        return self.rejection(route,oid) is None

    async def buy(self, oid, route, *, readiness=None):
        row=self.store.order(oid)
        if row['state']!='reserved' or row['mode']!=self.config.mode:
            return
        with self.store.db('trading') as c:
            c.execute('INSERT OR IGNORE INTO order_routes VALUES (?,?)',(oid,json.dumps(route.request())))
        self.store.update_order(oid,reason=MARKER)
        began=time.monotonic()
        timings={}
        simulation_source=None
        builder_timing=None
        def mark(name):timings[name]=round((time.monotonic()-began)*1000,3)
        def require_fresh():
            reason=self.rejection(route,oid)
            if reason:raise ValueError(reason)
        signed=False
        stage='precheck'
        signature=None
        self.priority.active+=1
        try:
            if self.store.rows('trading',"SELECT 1 FROM positions WHERE mint=? AND mode=? AND amount!='0'",(row['mint'],row['mode'])):
                raise ValueError('position-already-exists')
            require_fresh()
            mark('precheck_ms')
            # A dry run may simulate with the observed funded address when no
            # local public wallet is configured. It never reads that wallet's key.
            wallet=self.config.wallet_address or route.trade.wallet
            if hasattr(self.builder,"set_priority"):
                await self.builder.set_priority(self.priority.active)
            stage='build'
            result=await self.builder(self.config,route,wallet)
            builder_timing=result.get('buildTiming')
            mark('built_ms')
            require_fresh()
            wire=base64.b64decode(result['transaction'],validate=True)
            tx=VersionedTransaction.from_bytes(wire)
            if (len(wire)>1232 or tx.message.header.num_required_signatures!=1
                or str(tx.message.account_keys[0])!=wallet or result['wallet']!=wallet
                or result['mint']!=row['mint'] or int(result['amount'])!=int(row['amount'])
                or not 0<int(result['minOut'])<=int(result['quotedOut'])):
                raise ValueError('invalid-built-transaction')
            self.store.update_order(oid,min_out=result['minOut'],quoted_out=result['quotedOut'])
            # The routed buy spends quoteIn on the final token leg, not its SOL budget.
            quote_input=int(result['quoteIn'])
            if quote_input<=0:raise ValueError('invalid-entry-quote')
            with self.store.db('trading') as c:
                c.execute('INSERT OR REPLACE INTO buy_quote_inputs VALUES (?,?,?)',
                          (oid,route.quote_mint,str(quote_input)))
            stage='simulate'
            if reusable_simulation(result,tx,slot=route.trade.slot,commitment=self.config.hotlist_commitment):
                simulation_source='sdk-reused'
            else:
                simulation_source='python-fallback'
                simulation=await self.rpc.call('simulateTransaction',[result['transaction'],
                    {'encoding':'base64','sigVerify':False,'commitment':self.config.hotlist_commitment,'minContextSlot':route.trade.slot}])
                if simulation['value']['err'] is not None:
                    raise ValueError('simulation-rejected')
            mark('simulated_ms')
            require_fresh()
            # Prepare while subscription ACKs arrive; recheck before signing.
            stage='monitor'
            monitor_began=time.monotonic()
            if readiness is not None and not await readiness:
                raise ValueError('dev-sell-monitor-unavailable')
            timings['monitor_wait_ms']=round((time.monotonic()-monitor_began)*1000,3)
            mark('monitor_ready_ms')
            require_fresh()
            if self.config.dry_run:
                with self.store.db('trading') as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute("UPDATE orders SET state='dry-simulated',updated=? WHERE id=?",(time.time(),oid))
                    c.execute("UPDATE signals SET state='dry-simulated' WHERE order_id=?",(oid,))
                    c.commit()
                trade_record(self.store,route.trade,'execution','dry-run','simulation-passed',{'order':oid,'timing_ms':timings,'simulation_source':simulation_source})
                self.notices.emit('buy dry simulation passed',{'order':oid,'mint':row['mint'],
                    'min_out':result['minOut'],'quote_spend':result['quoteIn'],'simulation_wallet':wallet},alert=True)
                return
            # The dotenv keypair stays in Python. No key material goes through Node,
            # provider request bodies, logs or the database.
            stage='sign'
            keypair=self.config.wallet_keypair
            if keypair is None:
                raise ValueError('wallet-private-key-required')
            if str(keypair.pubkey())!=wallet:
                raise ValueError('wallet-keypair-mismatch')
            require_fresh()
            tx=VersionedTransaction(tx.message,[keypair])
            raw=base64.b64encode(bytes(tx)).decode()
            signature=str(tx.signatures[0])
            # FULL durability before sending, including crash between send and
            # response. An ambiguous send never releases the mint reservation.
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL')
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE orders SET state='signed',raw=?,signature=?,last_height=?,updated=? WHERE id=?",
                          (raw,signature,int(result['lastHeight']),time.time(),oid))
                c.commit()
            signed=True
            mark('signed_ms')
            require_fresh()
            stage='submit'
            mark('submit_started_ms')
            # A successful SDK/fallback simulation already checked this plan.
            # Preserve slot, freshness and durable-signature gates; omit duplicate preflight.
            response=await self.rpc.call('sendTransaction',[raw,{'encoding':'base64','skipPreflight':True,
                'preflightCommitment':self.config.hotlist_commitment,'maxRetries':0,'minContextSlot':route.trade.slot}])
            mark('submit_ack_ms')
            if response!=signature:
                raise ValueError('send-signature-mismatch')
            self.store.update_order(oid,state='submitted')
            trade_record(self.store,route.trade,'execution','submitted','submitted',{'order':oid,'buy_signature':signature,'timing_ms':timings,
                          'simulation_source':simulation_source,'builder_timing':builder_timing,'skip_preflight':True})
            self.notices.emit('buy submitted',{'order':oid,'signature':signature,'mint':row['mint']})
            if hasattr(self,'dev_exit'):self.dev_exit.wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mark('failed_ms')
            fees=fee_context(exc,self.config)
            if signed:
                self.store.update_order(oid,state='unknown')
            else:
                self.store.fail_order(oid,failure_reason(exc))
            trade_record(self.store,route.trade,'execution','unknown' if signed else 'failed',failure_reason(exc),
                         {'order':oid,'execution_stage':stage,'signed':signed,'buy_signature':signature,'timing_ms':timings,
                          'simulation_source':simulation_source,'builder_timing':builder_timing,
                          'signal_age_s':time.time()-route.trade.time,'signal_max_age_s':self.config.signal_age,
                          'source_proof':Processed(self.store).row(route.trade.signature),**fees})
            self.notices.emit('buy deferred' if signed else 'buy rejected',
                {'order':oid,**buy_context(self.config,route),'stage':stage,'reason':failure_reason(exc),
                 'type':type(exc).__name__,'signed':signed,'signature':signature,
                 'outcome':'unknown' if signed else 'not-submitted',**fees},alert=True)
        finally:
            self.priority.active-=1
            if hasattr(self.builder,"set_priority"):
                try:
                    await self.builder.set_priority(self.priority.active)
                except Exception:
                    self.notices.emit("builder priority sync deferred",{},key="builder-priority")

    def recover_unsigned(self):
        # An interrupted build has no signed bytes to broadcast. Never turn an
        # old reservation into a fresh buy after a process restart.
        for row in self.store.rows('trading',"SELECT id,mint,mode FROM orders WHERE state='reserved' AND reason=?",(MARKER,)):
            self.store.fail_order(row['id'],'interrupted-before-signing')
            self.notices.emit('buy rejected',{'order':row['id'],'mint':row['mint'],'mode':row['mode'],
                'stage':'recovery','reason':'interrupted-before-signing','outcome':'not-submitted'},alert=True)

    async def reconcile_once(self):
        rows=self.store.rows('trading',"SELECT * FROM orders WHERE reason=? AND mode='live' "
            "AND state IN ('signed','submitted','unknown','confirmed')",(MARKER,))
        if not rows:
            return
        result=await self.reconcile_rpc.call('getSignatureStatuses',[[r['signature'] for r in rows],{'searchTransactionHistory':True}])
        for row,status in zip(rows,result['value']):
            if status and status.get('confirmationStatus')=='finalized':
                if status.get('err') is not None:
                    self.store.fail_order(row['id'],'finalized-transaction-error')
                    self.notices.emit('buy failed on chain',{'order':row['id'],'mint':row['mint'],'mode':row['mode'],'signature':row['signature'],
                        'stage':'finalized','reason':'finalized-transaction-error','outcome':'failed'},alert=True)
                    continue
                raw=await self.reconcile_rpc.transaction(row['signature'],commitment='finalized')
                if raw is None:
                    continue
                tx=Tx(raw)
                signed=VersionedTransaction.from_bytes(base64.b64decode(row['raw']))
                wallet=str(signed.message.account_keys[0])
                if tx.signature!=row['signature'] or wallet not in tx.signers:
                    raise ValueError('fill-transaction-mismatch')
                tokens=tx.owner_tokens('post',wallet,row['mint'])-tx.owner_tokens('pre',wallet,row['mint'])
                if tokens<int(row['min_out']):
                    self.notices.emit('buy fill requires review',{'order':row['id'],'mint':row['mint'],'signature':row['signature'],
                        'stage':'reconcile','reason':'received-below-min-out'},alert=True,key='fill-review:'+row['id'])
                    continue
                native=[r for r in decode_native(tx) if r.trade.wallet==wallet and r.trade.mint==row['mint']]
                quote=native[0].trade.quote if len(native)==1 else int(row['amount'])
                completed=buy_success_context(self.store,row,tx,wallet,tokens)
                if self.store.fill(row['id'],tokens,quote):
                    self.notices.emit('buy finalized',completed,alert=True)
            elif status and status.get('err') is None and status.get('confirmationStatus')=='confirmed':
                self.store.update_order(row['id'],state='confirmed')
            elif not status:
                # A pruned/lagging RPC is not proof a buy never landed. Keep the
                # signature and permanent reservation; do not re-sign/rebuy.
                height=await self.reconcile_rpc.call('getBlockHeight',[{'commitment':'finalized'}])
                if height>row['last_height']:
                    self.store.update_order(row['id'],state='unknown')
                    self.notices.emit('buy status unknown after expiry; reservation retained',
                        {'order':row['id'],'mint':row['mint'],'signature':row['signature'],
                         'stage':'reconcile','reason':'status-unknown-after-expiry','outcome':'unknown'},alert=True,key='buy-unknown:'+row['id'],interval=1800)

    async def reconcile(self):
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.notices.emit('buy reconciliation deferred',{'type':type(exc).__name__},alert=True,key='buy-reconcile')
            await asyncio.sleep(2)
