"""Durable, event-driven emergency exits for buys rejected by a late dev check."""
import asyncio
import base64
import json
import time
from solders.transaction import VersionedTransaction
from chain_common.transaction import Tx
from chain_common.primitives import WSOL
from trade_execution.builder import build_sell, BuildError


class DevExit:
    def __init__(self,config,store,rpc,notices,priority,builder=build_sell):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.builder=builder
        self.wake=asyncio.Event()
        self.next={}

    def update(self,buy_id,**fields):
        if not fields or set(fields)-{'state','amount','raw','signature','last_height','min_out','reason'}:
            raise ValueError('invalid-dev-exit-update')
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL')
            c.execute('UPDATE dev_exits SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE buy_id=?',
                      (*fields.values(),time.time(),buy_id))

    def notice(self,kind,buy,**detail):
        self.notices.emit(kind,{'mint':buy['mint'],'order':buy['id'],'mode':buy['mode'],**detail},
                          alert=True,key=kind+':'+buy['id'],interval=60)

    def discover(self):
        with self.store.db('trading') as c:
            c.execute("INSERT OR IGNORE INTO dev_exits(buy_id,state,updated) "
                "SELECT o.id,'waiting',? FROM orders o JOIN token_dev_holdings d ON d.mint=o.mint "
                "JOIN order_routes r ON r.order_id=o.id WHERE o.side='buy' AND o.mode=? AND d.state='blocked' "
                "AND o.state IN ('reserved','signed','submitted','unknown','confirmed','finalized','dry-simulated')",
                (time.time(),self.config.mode))

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
        if not buy['signature']:return  # Build may be in flight; pre-sign gate will cancel it.
        wallet=self.config.wallet_address
        signed_buy=VersionedTransaction.from_bytes(base64.b64decode(buy['raw'],validate=True))
        if str(signed_buy.message.account_keys[0])!=wallet:raise ValueError('exit-wallet-mismatch')
        raw=await self.rpc.transaction(buy['signature'],commitment='confirmed')
        if not raw:return  # Never sell someone else's tokens while our buy is unconfirmed.
        if raw.get('meta',{}).get('err') is not None:
            return  # Wait for the buy reconciler to prove finalized failure.
        tx=Tx(raw)
        if tx.signature!=buy['signature'] or wallet not in tx.signers:raise ValueError('exit-buy-proof-mismatch')
        amount=tx.owner_tokens('post',wallet,buy['mint'])-tx.owner_tokens('pre',wallet,buy['mint'])
        if amount<=0:raise ValueError('exit-buy-amount-unavailable')
        request={**request,'minSlot':max(tx.slot,request['minSlot'])}
        self.priority.active+=1
        try:
            result=await self.builder(self.config,request,wallet,amount)
            wire=base64.b64decode(result['transaction'],validate=True)
            unsigned=VersionedTransaction.from_bytes(wire)
            if (len(wire)>1232 or unsigned.message.header.num_required_signatures!=1
                    or str(unsigned.message.account_keys[0])!=wallet or result.get('side')!='sell'
                    or result['wallet']!=wallet or result['mint']!=buy['mint']
                    or result['quoteMint']!=request['quoteMint'] or int(result['amount'])!=amount
                    or not 0<int(result['minOut'])<=int(result['quotedOut'])):
                raise ValueError('invalid-exit-transaction')
            simulation=await self.rpc.call('simulateTransaction',[result['transaction'],
                {'encoding':'base64','sigVerify':False,'commitment':'confirmed','minContextSlot':tx.slot}])
            if simulation['value']['err'] is not None:raise ValueError('exit-simulation-rejected')
            keypair=self.config.wallet_keypair
            if keypair is None or str(keypair.pubkey())!=wallet:raise ValueError('exit-wallet-key-required')
            signed=VersionedTransaction(unsigned.message,[keypair])
            encoded=base64.b64encode(bytes(signed)).decode();signature=str(signed.signatures[0])
            # FULL commit before any send; a crash/timeout never permits new signing.
            self.update(buy['id'],state='signed',amount=str(amount),raw=encoded,signature=signature,
                        last_height=int(result['lastHeight']),min_out=result['minOut'],reason='dev-holdings-over-limit')
            try:
                sent=await self.rpc.call('sendTransaction',[encoded,{'encoding':'base64','skipPreflight':False,
                    'preflightCommitment':'confirmed','maxRetries':0,'minContextSlot':tx.slot}])
                if sent!=signature:raise ValueError('exit-send-signature-mismatch')
            except Exception:
                self.update(buy['id'],state='unknown')
                self.notice('dev exit unknown',buy,signature=signature)
                return
            self.update(buy['id'],state='submitted')
            self.notice('dev exit submitted',buy,signature=signature,amount=str(amount),quote_mint=request['quoteMint'])
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
            if request['quoteMint']!=WSOL:
                received=tx.owner_tokens('post',wallet,request['quoteMint'])-tx.owner_tokens('pre',wallet,request['quoteMint'])
                if received<int(job['min_out']):raise ValueError('exit-fill-below-minimum')
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
                await self.rpc.call('sendTransaction',[job['raw'],{'encoding':'base64','skipPreflight':False,
                    'preflightCommitment':'confirmed','maxRetries':0}])
                self.next[buy['id']]=time.monotonic()+5

    async def once(self):
        self.discover()
        jobs=self.store.rows('trading',"SELECT e.* FROM dev_exits e JOIN orders o ON o.id=e.buy_id "
                            "WHERE o.mode=? AND e.state NOT IN ('finalized','cancelled','dry-skipped')",(self.config.mode,))
        for job in jobs:
            if time.monotonic()<self.next.get(job['buy_id'],0):continue
            buy=self.store.order(job['buy_id'])
            request=json.loads(self.store.rows('trading','SELECT request FROM order_routes WHERE order_id=?',(buy['id'],))[0]['request'])
            try:await self.step(buy,job,request)
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.next[buy['id']]=time.monotonic()+5
                reason=str(exc) if isinstance(exc,BuildError) else type(exc).__name__
                self.notice('dev exit deferred',buy,reason=reason)

    async def run(self):
        while True:
            self.wake.clear()
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.notices.emit('dev exit worker deferred',{'type':type(exc).__name__},alert=True,key='dev-exit-worker')
            try:await asyncio.wait_for(self.wake.wait(),timeout=2)
            except asyncio.TimeoutError:pass
