"""Durable rule-driven exits using pool spot indexes and validated venue sells and finalized fills."""
import asyncio
import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from contextlib import suppress

from solders.transaction import VersionedTransaction
from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.strategy.positions import load_rules, select_rule
from features.strategy.market_index import SCALE, register_fill
from features.funding.work import WalletFlights
from trade_execution.builder import build_sell, spot_index
from trade_execution.quote_sweep import enqueue, recipe_for

MARKER = 'take-profit-v1'
ACTIVE = "('reserved','signed','submitted','unknown','confirmed')"


def reason(exc):
    text=str(exc)
    return text if 0<len(text)<80 and all(c.islower() or c=='-' for c in text) else type(exc).__name__


def quote_received(tx, wallet, quote):
    if quote != WSOL:
        return tx.owner_tokens('post',wallet,quote)-tx.owner_tokens('pre',wallet,quote)
    # Include retained WSOL and neutralize ATA rent creation/closure. Only this
    # signed receipt's wallet-owned accounts are included, never old balances.
    owned={key for phase in ('pre','post') for key,(owner,_,_) in tx.tokens(phase).items() if owner==wallet}
    return tx.delta(wallet)+int(tx.meta['fee'])+sum(tx.delta(key) for key in owned)


class TakeProfit:
    def __init__(self,config,store,rpc,notices,priority,*,flights=None,builder=build_sell,
                 quoter=spot_index,rules=None,reconcile_rpc=None):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.flights=flights or WalletFlights()
        self.builder,self.quoter=builder,quoter
        self.reconcile_rpc=reconcile_rpc or rpc
        path=Path(config.take_profit_rules_path)
        self.rules=load_rules(path if path.is_absolute() else config.root/path) if rules is None else rules
        self.next={}

    def notice(self,kind,row,**detail):
        identity=str(detail.get('order') or row.get('id') or row['mint'])
        if kind=='take profit deferred':identity=row['mint']+':'+str(detail.get('reason',''))
        self.notices.emit(kind,{'mint':row['mint'],'mode':row['mode'],**detail},alert=True,
                          key=kind+':'+identity,interval=300 if kind=='take profit deferred' else 60)

    def emergency(self,mint):
        return bool(self.store.rows('trading',"SELECT 1 FROM token_dev_holdings WHERE mint=? AND state='blocked'",(mint,))
            or self.store.rows('trading',"SELECT 1 FROM dev_exits e JOIN orders o ON o.id=e.buy_id "
                "WHERE o.mint=? AND o.mode=? AND e.state NOT IN ('finalized','cancelled','dry-skipped')",
                (mint,self.config.mode)))

    def context(self,position):
        buys=self.store.rows('trading',"SELECT o.*,r.request FROM orders o JOIN order_routes r ON r.order_id=o.id "
            "WHERE o.mint=? AND o.mode=? AND o.side='buy' AND o.state='finalized' ORDER BY o.created DESC LIMIT 1",
            (position['mint'],position['mode']))
        if not buys:raise ValueError('take-profit-buy-proof-unavailable')
        buy=buys[0];request=json.loads(buy['request'])
        signed=VersionedTransaction.from_bytes(base64.b64decode(buy['raw'],validate=True))
        if str(signed.message.account_keys[0])!=self.config.wallet_address:raise ValueError('take-profit-wallet-mismatch')
        with self.store.db('trading') as c:
            register_fill(c,buy,int(position['entry_tokens']),int(position['entry_quote']),buy['updated'])
            market=c.execute('SELECT * FROM position_market_indexes WHERE order_id=?',(buy['id'],)).fetchone()
        if not market:raise ValueError('take-profit-quote-basis-unavailable')
        if market['quote_mint']!=request['quoteMint']:raise ValueError('take-profit-quote-basis-mismatch')
        return request,dict(market)

    def spot(self,result,request):
        if (result.get('mint')!=request['mint'] or result.get('quoteMint')!=request['quoteMint']
            or int(result.get('indexScale',0))!=SCALE or int(result.get('spotIndex',0))<=0):
            raise ValueError('invalid-take-profit-spot-index')
        return int(result['spotIndex'])

    def poll_interval(self,pos,now=None):
        rows=self.store.rows('trading',"SELECT o.updated,m.registered FROM orders o LEFT JOIN position_market_indexes m "
            "ON m.order_id=o.id WHERE o.mint=? AND o.mode=? AND o.side='buy' AND o.state='finalized' "
            "ORDER BY o.created DESC LIMIT 1",(pos['mint'],pos['mode']))
        start=(rows[0]['registered'] if rows[0]['registered'] is not None else rows[0]['updated']) if rows else pos['updated']
        age=max(0,(time.time() if now is None else now)-start)
        return self.config.take_profit_poll_seconds*(5 if age>=1800 else 1)

    def validate_quote(self,result,request,amount):
        if (result.get('side')!='sell' or result.get('wallet')!=self.config.wallet_address
            or result.get('mint')!=request['mint'] or result.get('quoteMint')!=request['quoteMint']
            or int(result['amount'])!=amount or not 0<int(result['minOut'])<=int(result['quotedOut'])):
            raise ValueError('invalid-take-profit-quote')

    async def submit(self,position,request,rid,amount,*,entry_index):
        oid=self.store.reserve(self.config,position['mint'],position['pool'],amount,'sell',rid,reason=MARKER)
        if not oid:return
        with self.store.db('trading') as c:
            c.execute('INSERT INTO order_routes VALUES (?,?)',(oid,json.dumps(request)))
        signed=False
        self.priority.active+=1
        try:
            result=await self.builder(self.config,request,self.config.wallet_address,amount)
            self.validate_quote(result,request,amount)
            gain=(Decimal(self.spot(result,request))/entry_index-1)*100
            rule=next(rule for rule in self.rules if rule[0]==rid)
            if not select_rule([rule],position,gain,Decimal(position['peak'])):
                raise ValueError('take-profit-threshold-no-longer-met')
            wire=base64.b64decode(result['transaction'],validate=True)
            unsigned=VersionedTransaction.from_bytes(wire)
            if (len(wire)>1232 or unsigned.message.header.num_required_signatures!=1
                or str(unsigned.message.account_keys[0])!=self.config.wallet_address):
                raise ValueError('invalid-take-profit-transaction')
            simulation=await self.rpc.call('simulateTransaction',[result['transaction'],
                {'encoding':'base64','sigVerify':False,'commitment':'confirmed','minContextSlot':request['minSlot']}])
            if simulation['value']['err'] is not None:raise ValueError('take-profit-simulation-rejected')
            if self.emergency(position['mint']):raise ValueError('dev-exit-priority')
            key=self.config.wallet_keypair
            if self.config.dry_run or key is None or str(key.pubkey())!=self.config.wallet_address:
                raise ValueError('take-profit-live-wallet-required')
            tx=VersionedTransaction(unsigned.message,[key]);signature=str(tx.signatures[0])
            encoded=base64.b64encode(bytes(tx)).decode()
            # Persist signed bytes before any send. Unknown outcomes retain this reservation.
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE orders SET state='signed',raw=?,signature=?,last_height=?,min_out=?,quoted_out=?,updated=? WHERE id=?",
                    (encoded,signature,int(result['lastHeight']),str(result['minOut']),str(result['quotedOut']),time.time(),oid))
                c.commit()
            signed=True
            sent=await self.rpc.call('sendTransaction',[encoded,{'encoding':'base64','skipPreflight':False,
                'preflightCommitment':'confirmed','maxRetries':0,'minContextSlot':request['minSlot']}])
            if sent!=signature:raise ValueError('take-profit-send-mismatch')
            self.store.update_order(oid,state='submitted')
            self.notice('take profit submitted',position,order=oid,signature=signature,rule=rid,
                        rule_type=rule[1],thresholds={key:str(value) for key,value in rule[2].items()},
                        gain_percent=str(gain),amount=str(amount))
        except asyncio.CancelledError:
            if not signed:self.store.fail_order(oid,'take-profit-interrupted-before-sign')
            raise
        except Exception as exc:
            if signed:self.store.update_order(oid,state='unknown')
            else:self.store.fail_order(oid,'take-profit-build-failed')
            self.notice('take profit unknown' if signed else 'take profit deferred',position,order=oid,
                        reason=reason(exc))
        finally:
            self.priority.active-=1

    async def reconcile(self,order):
        rpc=self.reconcile_rpc
        signed=VersionedTransaction.from_bytes(base64.b64decode(order['raw'],validate=True))
        if str(signed.message.account_keys[0])!=self.config.wallet_address:
            raise ValueError('take-profit-wallet-mismatch')
        response=await rpc.call('getSignatureStatuses',[[order['signature']],{'searchTransactionHistory':True}])
        status=response['value'][0]
        if not status:
            height=await rpc.call('getBlockHeight',[{'commitment':'finalized'}])
            if height>order['last_height']:
                self.store.update_order(order['id'],state='unknown')
                self.notice('take profit unknown',order,signature=order['signature'],reason='status-unknown-after-expiry')
            else:
                # Rebroadcast the SAME bytes only; there is never a replacement signature.
                await self.rpc.call('sendTransaction',[order['raw'],{'encoding':'base64','skipPreflight':False,
                    'preflightCommitment':'confirmed','maxRetries':0}])
            return
        if status.get('confirmationStatus')!='finalized':return
        if status.get('err') is not None:
            self.store.fail_order(order['id'],'take-profit-finalized-failure')
            self.notice('take profit deferred',order,signature=order['signature'],reason='finalized-failure')
            return
        raw=await rpc.transaction(order['signature'],commitment='finalized')
        if raw is None:return
        tx=Tx(raw);wallet=self.config.wallet_address
        if tx.signature!=order['signature'] or tx.keys[0]!=wallet or wallet not in tx.signers:
            raise ValueError('take-profit-fill-mismatch')
        request=json.loads(self.store.rows('trading','SELECT request FROM order_routes WHERE order_id=?',(order['id'],))[0]['request'])
        sold=tx.owner_tokens('pre',wallet,order['mint'])-tx.owner_tokens('post',wallet,order['mint'])
        received=quote_received(tx,wallet,request['quoteMint'])
        if sold!=int(order['amount']) or received<int(order['min_out']):raise ValueError('take-profit-fill-amount-mismatch')
        recipe=recipe_for(self.store,request)
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state FROM orders WHERE id=?',(order['id'],)).fetchone()
            if current['state']=='finalized':return
            pos=c.execute('SELECT * FROM positions WHERE mint=? AND mode=?',(order['mint'],order['mode'])).fetchone()
            if not pos or int(pos['amount'])<sold:raise ValueError('take-profit-exceeds-position')
            done=json.loads(pos['rules']);done[order['rule_id']]='done'
            c.execute('UPDATE positions SET amount=?,rules=?,updated=? WHERE mint=? AND mode=?',
                      (str(int(pos['amount'])-sold),json.dumps(done),time.time(),order['mint'],order['mode']))
            c.execute("UPDATE orders SET state='finalized',updated=? WHERE id=?",(time.time(),order['id']))
            if request['quoteMint']!=WSOL:
                enqueue(c,'tp:'+order['id'],wallet,request['quoteMint'],request['quoteProgram'],received,tx.slot,recipe)
            c.commit()
        self.notice('take profit finalized',order,signature=order['signature'],amount=str(sold),received=str(received),quote_mint=request['quoteMint'])

    async def background_quote(self,request,amount):
        if self.priority.active:return None
        task=asyncio.create_task(self.quoter(self.config,request,self.config.wallet_address,amount))
        try:
            while not task.done():
                if self.priority.active or self.emergency(request['mint']):return None
                await asyncio.wait({task},timeout=0.05)
            return task.result()
        finally:
            if not task.done():task.cancel()
            with suppress(asyncio.CancelledError,Exception):await task

    async def position(self,pos):
        if self.emergency(pos['mint']):return
        request,market=self.context(pos)
        amount=int(pos['amount'])
        result=await self.background_quote(request,amount)
        if result is None:return
        current=self.spot(result,request)
        if self.priority.active or self.emergency(pos['mint']):return
        gain=(Decimal(current)/int(market['entry_index'])-1)*100
        previous=(Decimal(market['peak_index'])/int(market['entry_index'])-1)*100
        peak=max(previous,gain)
        done=json.loads(pos['rules'])
        # Match RH: a completed trailing stage rearms only at a new observed high.
        if peak>previous:
            for rid,kind,_ in self.rules:
                if kind=='trailing':done.pop(rid,None)
        with self.store.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE position_market_indexes SET peak_index=?,last_index=? WHERE order_id=?',
                      (str(max(current,int(market['peak_index']))),str(current),market['order_id']))
            c.execute('UPDATE positions SET peak=?,rules=? WHERE mint=? AND mode=?',
                      (str(peak),json.dumps(done),pos['mint'],pos['mode']))
            c.commit()
        pos={**pos,'peak':str(peak),'rules':json.dumps(done)}
        selected=select_rule(self.rules,pos,gain,peak)
        if selected:await self.submit(pos,request,*selected,entry_index=int(market['entry_index']))

    async def once(self):
        if self.config.dry_run:return  # No synthetic fills or real sells in dry mode.
        orders=self.store.rows('trading',f"SELECT * FROM orders WHERE side='sell' AND reason=? AND mode=? AND state IN {ACTIVE}",
                               (MARKER,self.config.mode))
        for order in orders:
            async with self.flights.hold(order['mint']):
                if not order['signature']:
                    self.store.fail_order(order['id'],'take-profit-interrupted-before-sign')
                else:
                    try:await self.reconcile(order)
                    except asyncio.CancelledError:raise
                    except Exception as exc:self.notice('take profit deferred',order,reason=type(exc).__name__)
        if not self.rules or self.priority.active:return
        positions=self.store.rows('trading',"SELECT * FROM positions WHERE mode=? AND amount!='0'",(self.config.mode,))
        for pos in positions:
            if self.priority.active:break
            if time.monotonic()<self.next.get(pos['mint'],0):continue
            self.next[pos['mint']]=time.monotonic()+self.poll_interval(pos)
            async with self.flights.hold(pos['mint']):
                if self.store.rows('trading',f"SELECT 1 FROM orders WHERE mint=? AND mode=? AND side='sell' AND state IN {ACTIVE}",
                                   (pos['mint'],pos['mode'])):continue
                # Emergency reconciliation may have changed the amount while we waited.
                pos=self.store.rows('trading','SELECT * FROM positions WHERE mint=? AND mode=?',(pos['mint'],pos['mode']))[0]
                if int(pos['amount'])<=0:continue
                try:await self.position(pos)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    self.notice('take profit deferred',pos,reason=reason(exc))

    async def run(self):
        while True:
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception as exc:self.notices.emit('take profit worker deferred',{'type':type(exc).__name__},alert=True,key='tp-worker')
            await asyncio.sleep(self.config.take_profit_poll_seconds)
