"""Patient quote-to-SOL conversion; foreground trades always take precedence."""
import asyncio
import base64
import json
import time
from contextlib import suppress
from solders.transaction import VersionedTransaction
from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.database.quote_cache import SeenTokens
from trade_execution.builder import build_sweep, BuildError


def recipe_for(store,request):
    if request['quoteMint']==WSOL:return None
    recipe=SeenTokens(store).recipe(request['quoteMint']) or request.get('swapRecipe')
    if not recipe and request['route']=='meteora_dlmm_to_pump_curve':
        recipe={'version':1,'tables':request['lookupTables'],'steps':[{'label':'Meteora DLMM',
            'pool':request['pool'],'inputMint':WSOL,'outputMint':request['quoteMint']}]}
    return recipe


def enqueue(c,identifier,wallet,mint,program,amount,slot,recipe):
    if amount<=0:return
    c.execute('INSERT OR IGNORE INTO quote_sweeps(id,wallet,mint,program,amount,min_slot,recipe,updated) '
              'VALUES (?,?,?,?,?,?,?,?)',(identifier,wallet,mint,program,str(amount),slot,
                                         json.dumps(recipe) if recipe else None,time.time()))


class ForegroundBusy(Exception):
    pass


class QuoteSweep:
    def __init__(self,config,store,rpc,notices,priority,builder=build_sweep):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.builder=builder
        self.next={}

    def busy(self):
        if self.priority.active:return True
        return bool(self.store.rows('trading',"SELECT 1 FROM orders WHERE mode=? AND state IN "
            "('reserved','signed','submitted','unknown','confirmed') LIMIT 1",(self.config.mode,))
            or self.store.rows('trading',"SELECT 1 FROM dev_exits e JOIN orders o ON o.id=e.buy_id "
                "WHERE o.mode=? AND e.state NOT IN ('finalized','cancelled','dry-skipped') LIMIT 1",(self.config.mode,)))

    def idle(self):
        if self.busy():raise ForegroundBusy()

    async def idle_work(self,coroutine):
        task=asyncio.create_task(coroutine)
        try:
            while not task.done():
                self.idle()
                await asyncio.wait({task},timeout=0.05)
            self.idle()
            return task.result()
        finally:
            if not task.done():task.cancel()
            with suppress(asyncio.CancelledError,Exception):await task

    def update(self,identifier,**fields):
        if not fields or set(fields)-{'state','raw','signature','last_height','min_out','reason','recipe'}:
            raise ValueError('invalid-sweep-update')
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL')
            c.execute('UPDATE quote_sweeps SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE id=?',
                      (*fields.values(),time.time(),identifier))

    def notice(self,kind,job,**detail):
        self.notices.emit(kind,{'mint':job['mint'],'amount':job['amount'],'task':job['id'],**detail},
                          alert=True,key=kind+':'+job['id'],interval=300)

    async def step(self,job):
        self.idle()
        if job['wallet']!=self.config.wallet_address:raise ValueError('sweep-wallet-changed')
        if job['signature']:
            await self.reconcile(job)
            return
        recipe=json.loads(job['recipe']) if job['recipe'] else SeenTokens(self.store).recipe(job['mint'])
        if job['mint']!=WSOL and not recipe:raise BuildError('price-cache-miss')
        request={'route':'quote_to_sol','mint':job['mint'],'tokenProgram':job['program'],
                 'minSlot':job['min_slot'],'lookupTables':[],'swapRecipe':recipe}
        result=await self.idle_work(self.builder(self.config,request,job['wallet'],int(job['amount'])))
        wire=base64.b64decode(result['transaction'],validate=True)
        unsigned=VersionedTransaction.from_bytes(wire)
        if (len(wire)>1232 or unsigned.message.header.num_required_signatures!=1
                or str(unsigned.message.account_keys[0])!=job['wallet'] or result.get('side')!='sweep'
                or result['wallet']!=job['wallet'] or result['mint']!=job['mint']
                or int(result['amount'])!=int(job['amount']) or not 0<int(result['minOut'])<=int(result['quotedOut'])):
            raise ValueError('invalid-sweep-transaction')
        metrics=result['risk']
        if not 0<=int(metrics['feePpm'])<=self.config.max_total_fee_bps*100:raise BuildError('total-fee-limit')
        if not 0<=int(metrics['impactPpm'])<=self.config.max_price_impact_bps*100:raise BuildError('price-impact-limit')
        def fresh():
            if not 0<=time.time()*1000-result['quoteTime']<=5000:raise BuildError('sweep-quote-expired')
        fresh()
        simulation=await self.idle_work(self.rpc.call('simulateTransaction',[result['transaction'],
            {'encoding':'base64','sigVerify':False,'commitment':'confirmed','minContextSlot':job['min_slot']}]))
        if simulation['value']['err'] is not None:raise BuildError('simulation-rejected')
        self.idle();fresh()
        key=self.config.wallet_keypair
        if key is None or str(key.pubkey())!=job['wallet']:raise ValueError('sweep-wallet-key-required')
        signed=VersionedTransaction(unsigned.message,[key]);signature=str(signed.signatures[0])
        encoded=base64.b64encode(bytes(signed)).decode()
        self.update(job['id'],state='signed',raw=encoded,signature=signature,last_height=int(result['lastHeight']),
                    min_out=result['minOut'],recipe=json.dumps(recipe) if recipe else None)
        # No await between final foreground check, durable signing and starting send.
        # Once sent, a new foreground trade cannot recall this signature.
        try:
            response=await self.rpc.call('sendTransaction',[encoded,{'encoding':'base64','skipPreflight':False,
                'preflightCommitment':'confirmed','maxRetries':0,'minContextSlot':job['min_slot']}])
            if response!=signature:raise ValueError('sweep-signature-mismatch')
        except Exception:
            self.update(job['id'],state='unknown')
            self.notice('quote sweep unknown',job,signature=signature)
            return
        self.update(job['id'],state='submitted')
        self.notice('quote sweep submitted',job,signature=signature,**metrics)

    async def reconcile(self,job):
        result=await self.idle_work(self.rpc.call('getSignatureStatuses',[[job['signature']],{'searchTransactionHistory':True}]))
        status=result['value'][0]
        if status and status.get('confirmationStatus')=='finalized':
            if status.get('err') is not None:
                self.update(job['id'],state='waiting',raw=None,signature=None,last_height=None,reason='sweep-finalized-failure')
                self.next[job['id']]=time.monotonic()+30
                self.notice('quote sweep deferred',job,reason='sweep-finalized-failure')
                return
            raw=await self.idle_work(self.rpc.transaction(job['signature'],commitment='finalized'))
            if not raw:return
            tx=Tx(raw);wallet=job['wallet']
            if tx.signature!=job['signature'] or wallet not in tx.signers:raise ValueError('sweep-fill-mismatch')
            spent=tx.owner_tokens('pre',wallet,job['mint'])-tx.owner_tokens('post',wallet,job['mint'])
            if spent!=int(job['amount']):raise ValueError('sweep-spend-mismatch')
            # Fresh intermediate ATAs cost rent. The temporary WSOL account is
            # created and closed atomically, so its rent cancels out.
            new_rent=sum(int(tx.meta['postBalances'][i]) for i,k in enumerate(tx.keys)
                if tx.meta['preBalances'][i]==0 and tx.tokens('post').get(k,('',))[0]==wallet)
            received=tx.delta(wallet)+int(tx.meta['fee'])+new_rent
            if received<int(job['min_out']):raise ValueError('sweep-sol-below-minimum')
            recipe=json.loads(job['recipe']) if job['recipe'] else None
            children=[]
            # Conservative multi-hop min-outs can leave positive intermediate
            # dust. Queue only this transaction's gain, never old wallet balances.
            for i,step in enumerate((recipe or {}).get('steps',[])[:-1]):
                mint=step['outputMint']
                gained=tx.owner_tokens('post',wallet,mint)-tx.owner_tokens('pre',wallet,mint)
                if gained>0:
                    programs={r.get('programId') for r in tx.meta.get('postTokenBalances',[]) if r.get('owner')==wallet and r['mint']==mint}
                    if len(programs)!=1 or None in programs:raise ValueError('sweep-intermediate-program-missing')
                    children.append((mint,programs.pop(),gained,{**recipe,'steps':recipe['steps'][:i+1]}))
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
                for mint,program,amount,path in children:
                    enqueue(c,job['id']+':'+mint,wallet,mint,program,amount,tx.slot,path)
                c.execute("UPDATE quote_sweeps SET state='finalized',updated=? WHERE id=?",(time.time(),job['id']))
                c.commit()
            self.notice('quote sweep finalized',job,signature=job['signature'],sol_lamports=str(received))
        elif not status:
            height=await self.idle_work(self.rpc.call('getBlockHeight',[{'commitment':'finalized'}]))
            if height>job['last_height']:
                self.update(job['id'],state='unknown',reason='sweep-status-unknown-after-expiry')
                self.notice('quote sweep unknown',job,signature=job['signature'],reason='sweep-status-unknown-after-expiry')
                self.next[job['id']]=time.monotonic()+60
            else:
                self.idle()
                await self.rpc.call('sendTransaction',[job['raw'],{'encoding':'base64','skipPreflight':False,
                    'preflightCommitment':'confirmed','maxRetries':0}])
                self.next[job['id']]=time.monotonic()+10

    async def once(self):
        if self.config.dry_run or self.busy():return
        jobs=self.store.rows('trading',"SELECT * FROM quote_sweeps WHERE state!='finalized' ORDER BY updated")
        signed=[job for job in jobs if job['signature']]
        if signed:jobs=signed[:1]
        for job in jobs:
            if self.busy():return
            if time.monotonic()<self.next.get(job['id'],0):continue
            try:await self.step(job)
            except ForegroundBusy:return
            except asyncio.CancelledError:raise
            except Exception as exc:
                reason=str(exc) if isinstance(exc,BuildError) else type(exc).__name__
                self.next[job['id']]=time.monotonic()+30
                self.notice('quote sweep deferred',job,reason=reason)
            # Do not overlap two conversions of the same quote balance.
            pending=self.store.rows('trading',"SELECT 1 FROM quote_sweeps WHERE signature IS NOT NULL AND state!='finalized' LIMIT 1")
            if pending:return

    async def run(self):
        while True:
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.notices.emit('quote sweep worker deferred',{'type':type(exc).__name__},alert=True,key='sweep-worker')
            await asyncio.sleep(2)
