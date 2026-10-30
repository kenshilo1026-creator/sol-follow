"""Confirmed dev sale evidence and durable event-driven full-position exit triggers.

ABIs follow the pinned Pump/PumpSwap IDLs and existing LaunchLab/CPMM builders.
A token transfer must occur INSIDE the successful venue sell invocation. Token
ownership comes from receipt metadata, not the instruction's arbitrary user label.
"""
import asyncio
from collections import deque
import json
import time
from chain_common.primitives import TOKEN,TOKEN_2022,ata,b58decode,discriminator
from chain_common.transaction import Tx
from launchpads import pump_fun,stonk
from features.strategy.startup_positions import retire_empty_position

CPMM='CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C'


def sell_leg(tx,ix,mint):
    raw=b58decode(ix.get('data',''));a=tx.accounts(ix);program=ix.get('programId')
    if len(raw)<24:return None
    tag=raw[:8]
    if program==pump_fun.PROGRAM:
        if tag==discriminator('global','sell') and len(a)>=12 and a[2]==mint and a[3]==pump_fun.curve_address(mint):
            return a[5],a[4]
        if tag==discriminator('global','sell_v2') and len(a)>=26 and a[1]==mint and a[10]==pump_fun.curve_address(mint):
            return a[14],a[11]
    elif program==stonk.PROGRAM and tag in {discriminator('global',n) for n in ('sell_exact_in','sell_exact_out')}:
        if len(a)>=15 and a[9]==mint and a[3]==stonk.PLATFORM and a[4]==stonk.pool_address(mint,a[10]):
            return a[5],a[7]
    elif program==pump_fun.SWAP_PROGRAM and tag==discriminator('global','sell'):
        if len(a)>=17 and a[3]==mint:return a[5],a[7]
    elif program==CPMM and tag in {discriminator('global',n) for n in ('swap_base_input','swap_base_output')}:
        if len(a)>=13 and a[10]==mint:return a[4],a[6]
    return None


def children(tx,path,ix):
    """Never borrow a sibling swap's transfers to attribute an unrelated send."""
    parts=path.split('.');inner=tx.inner.get(int(parts[0]),[])
    if len(parts)==1:return inner
    height=ix.get('stackHeight')
    if type(height) is not int or height<2:raise ValueError('dev-sell-cpi-depth-unavailable')
    result=[]
    for child in inner[int(parts[1])+1:]:
        depth=child.get('stackHeight')
        if type(depth) is not int:raise ValueError('dev-sell-cpi-depth-unavailable')
        if depth<=height:break
        result.append(child)
    return result


def transfer(tx,ix):
    if ix.get('programId') not in (TOKEN,TOKEN_2022):return None
    parsed=ix.get('parsed',{});info=parsed.get('info',{})
    if parsed.get('type')=='transfer':return info['source'],info['destination'],int(info['amount'])
    if parsed.get('type') in ('transferChecked','transferCheckedWithFee'):
        return info['source'],info['destination'],int(info['tokenAmount']['amount'])
    a=tx.accounts(ix);data=b58decode(ix.get('data',''))
    if len(data)==9 and data[0]==3 and len(a)>=3:return a[0],a[1],int.from_bytes(data[1:9],'little')
    if len(data)==10 and data[0]==12 and len(a)>=4:return a[0],a[2],int.from_bytes(data[1:9],'little')
    # Token-2022 TransferFeeExtension / TransferCheckedWithFee.
    if len(data)==19 and data[:2]==bytes([26,1]) and len(a)>=4:
        return a[0],a[2],int.from_bytes(data[2:10],'little')
    return None


def sold_amount(tx,dev,mint):
    pre,post=tx.tokens('pre'),tx.tokens('post');sold=0
    for path,ix in tx.instructions():
        leg=sell_leg(tx,ix,mint)
        if not leg:continue
        source,vault=leg
        owners=[values[source][:2] for values in (pre,post) if source in values]
        if not owners or any(owner!=(dev,mint) for owner in owners):continue
        vaults=[values[vault][1] for values in (pre,post) if vault in values]
        if not vaults or any(value!=mint for value in vaults):continue
        for child in children(tx,path,ix):
            moved=transfer(tx,child)
            if moved and moved[:2]==(source,vault) and moved[2]>0:sold+=moved[2]
    return sold


class DevSell:
    def __init__(self,config,store,rpc,notices,exit_engine):
        self.config,self.store,self.rpc,self.notices,self.exit=config,store,rpc,notices,exit_engine
        self.wake=asyncio.Event();self.watches={}
        # Bounded ingress hints bridge create-worker scheduling. These are not
        # sale proofs: the durable consumer still verifies confirmed receipts.
        self.recent=deque(maxlen=2048)
        self.history={};self.history_latest={};self.history_wake=asyncio.Event()

    def register(self,oid,route):
        """Install one reserved buy locally, without a global scan or RPC."""
        buy=self.store.order(oid)
        dev=route.trade.wallet
        item={**buy,'dev':dev,'create_slot':route.trade.slot}
        accounts={dev,ata(dev,buy['mint'],route.request()['tokenProgram'])}
        for account in accounts:
            current=self.watches.setdefault(account,[])
            if not any(w['id']==oid for w in current):current.append(item)
        # Persist early events before yielding to the builder. No receipt read.
        self.prune_recent()
        for _,signature,slot,keys in self.recent:
            if signature!=route.trade.signature:
                matched=accounts.intersection(keys)
                if matched:self.market_events(matched,signature,slot)
        for account in accounts:self.subscribed(account)

    def prune_recent(self):
        cutoff=time.monotonic()-30
        while self.recent and self.recent[0][0]<cutoff:self.recent.popleft()

    def stream_event(self,signature,slot,keys):
        # Receive even before local registration and outside the buy live window:
        # an older sell may still require an exit from an existing position.
        self.prune_recent()
        keys=frozenset(keys)
        self.recent.append((time.monotonic(),signature,slot,keys))
        self.market_events(self.watches.keys() & keys,signature,slot)

    def reconnected(self):
        # Live-only gRPC cannot replay a disconnected interval. Existing bounded
        # confirmed history catch-up repairs it independently of entry execution.
        for address in self.watches:self.subscribed(address)

    def watch_addresses(self):
        rows=self.store.rows('trading',"SELECT o.*,r.request FROM orders o JOIN order_routes r ON r.order_id=o.id "
            "LEFT JOIN positions p ON p.mint=o.mint AND p.mode=o.mode WHERE o.side='buy' AND o.mode=? "
            "AND (o.state IN ('reserved','signed','submitted','unknown','confirmed','dry-simulated') "
            "OR o.state='finalized' AND p.amount!='0') AND NOT EXISTS (SELECT 1 FROM dev_exits e WHERE e.buy_id=o.id)",(self.config.mode,))
        watches={}
        for buy in rows:
            watches_saved=self.store.rows('trading','SELECT * FROM dev_sell_watches WHERE buy_id=?',(buy['id'],))
            if not watches_saved:
                # Backfill old create entries from actual saved creation evidence,
                # never mistake a legacy buying wallet for the token's dev.
                origins=self.store.rows('trading','SELECT * FROM order_wallets WHERE order_id=?',(buy['id'],))
                if len(origins)!=1:continue
                origin=origins[0]
                creates=self.store.rows('funding','SELECT 1 FROM launch_creates WHERE mint=? AND user=? AND signature=? AND slot=?',
                    (buy['mint'],origin['wallet'],origin['signature'],origin['slot']))
                if not creates:continue
                with self.store.db('trading') as c:
                    c.execute('INSERT OR IGNORE INTO dev_sell_watches VALUES (?,?,?)',(buy['id'],origin['wallet'],origin['slot']))
                watches_saved=[{'dev':origin['wallet'],'create_slot':origin['slot']}]
            saved=watches_saved[0];dev=saved['dev'];request=json.loads(buy['request'])
            item={**buy,'dev':dev,'create_slot':saved['create_slot']}
            for account in (dev,ata(dev,buy['mint'],request['tokenProgram'])):
                watches.setdefault(account,[]).append(item)
        added=set(watches)-set(self.watches)
        self.watches=watches
        for address in added:self.subscribed(address)
        return set(watches)

    def subscribed(self,address):
        # Initial registration covers an already subscribed hotlist dev; an ACK
        # requests another pass to close the gap before the subscription existed.
        if address not in self.watches:return
        if address in self.history:
            self.history[address]['again']=True
        else:
            self.history[address]={'first':time.time(),'due':0,'pages':0,'before':None,
                                   'until':self.history_latest.get(address),'latest':None,'again':False}
        self.history_wake.set()

    async def catch_up_once(self):
        for address,job in list(self.history.items()):
            if address not in self.watches:
                self.history.pop(address,None);continue
            if job['due']>time.time():continue
            try:
                options={'limit':100,'commitment':'confirmed'}
                for key in ('before','until'):
                    if job[key]:options[key]=job[key]
                rows=await self.rpc.call('getSignaturesForAddress',[address,options])
                if not isinstance(rows,list) or any(not isinstance(r,dict) or type(r.get('slot')) is not int
                        or not isinstance(r.get('signature'),str) or not r['signature'] for r in rows):
                    raise ValueError('dev-sell-history-invalid')
                floor=min(b['create_slot'] for b in self.watches.get(address,()))
                if rows and job['latest'] is None:job['latest']=rows[0]['signature']
                for row in rows:
                    if row.get('err') is None and row['slot']>=floor:
                        self.market_event(address,row['signature'],row['slot'])
                if rows and rows[-1]['signature']==job['before']:raise ValueError('dev-sell-history-stalled')
                job['pages']+=1
                more=len(rows)==100 and rows[-1]['slot']>=floor
                if more and job['pages']<20:
                    job['before']=rows[-1]['signature']
                else:
                    if more:
                        self.notices.emit('dev sell catch-up incomplete',{'address':address,'reason':'page-limit'},
                                          alert=True,key='dev-sell-gap:'+address)
                    elif job['latest']:self.history_latest[address]=job['latest']
                    self.history.pop(address,None)
                    if job['again']:self.subscribed(address)
            except asyncio.CancelledError:raise
            except Exception as exc:
                if time.time()-job['first']>=600:
                    self.history.pop(address,None)
                    self.notices.emit('dev sell catch-up incomplete',{'address':address,'reason':type(exc).__name__},
                                      alert=True,key='dev-sell-gap:'+address)
                else:job['due']=min(job['first']+600,time.time()+5)
            return  # One bounded page per turn; live receipt decoding is separate.

    async def catch_up(self):
        while True:
            self.history_wake.clear()
            await self.catch_up_once()
            try:await asyncio.wait_for(self.history_wake.wait(),timeout=0.2)
            except asyncio.TimeoutError:pass

    def market_event(self,address,signature,slot):
        self.market_events((address,),signature,slot)

    def market_events(self,addresses,signature,slot):
        if not isinstance(signature,str) or not signature or type(slot) is not int or slot<=0:return
        buys={buy['id']:buy for address in addresses for buy in self.watches.get(address,())
              if slot>=buy['create_slot']}
        if not buys:return
        now=time.time()
        # A stream transaction usually matches both dev and ATA. Persist all
        # affected buys in one commit rather than fsync twice for the same event.
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            c.executemany('INSERT OR IGNORE INTO dev_sell_events(signature,buy_id,slot,due,first_seen) VALUES (?,?,?,?,?)',
                          [(signature,buy_id,slot,now,now) for buy_id in buys])
            c.commit()
        self.wake.set()

    async def once(self):
        rows=self.store.rows('trading',"SELECT e.* FROM dev_sell_events e JOIN orders o ON o.id=e.buy_id "
            "WHERE o.mode=? AND e.state='pending' AND e.due<=? ORDER BY e.slot DESC,e.first_seen LIMIT 16",(self.config.mode,time.time()))
        receipts={}
        for event in rows:
            buy=self.store.order(event['buy_id']);signature=event['signature']
            try:
                if self.store.rows('trading','SELECT 1 FROM dev_exits WHERE buy_id=?',(buy['id'],)):
                    with self.store.db('trading') as c:
                        c.execute("UPDATE dev_sell_events SET state='ignored',reason='exit-already-armed' WHERE signature=? AND buy_id=?",(signature,buy['id']))
                    continue
                if self.store.rows('funding','SELECT 1 FROM dead_slots WHERE slot=?',(event['slot'],)):
                    with self.store.db('trading') as c:
                        c.execute("UPDATE dev_sell_events SET state='ignored',reason='dead-slot' WHERE signature=? AND buy_id=?",
                                  (signature,buy['id']))
                    continue
                if signature not in receipts:receipts[signature]=await self.rpc.transaction(signature,commitment='confirmed')
                raw=receipts[signature]
                if raw is None:raise ValueError('dev-sell-not-indexed')
                if raw.get('meta',{}).get('err') is not None:
                    amount=0
                else:
                    tx=Tx(raw)
                    if tx.signature!=signature or tx.slot!=event['slot']:raise ValueError('dev-sell-receipt-mismatch')
                    origins=self.store.rows('trading','SELECT dev,create_slot FROM dev_sell_watches WHERE buy_id=?',(buy['id'],))
                    amount=(sold_amount(tx,origins[0]['dev'],buy['mint']) if len(origins)==1 and tx.slot>=origins[0]['create_slot'] else 0)
                if amount and await retire_empty_position(self.config,self.store,self.rpc,self.notices,buy['mint']):
                    # No exit alert or unsigned job for an externally emptied
                    # position. Refresh cached watches as well as durable rows.
                    with self.store.db('trading') as c:
                        c.execute("UPDATE dev_sell_events SET state='ignored',reason='wallet-balance-zero' WHERE signature=? AND buy_id=?",
                                  (signature,buy['id']))
                    self.watch_addresses()
                    continue
                with self.store.db('trading') as c:
                    c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
                    if amount:
                        c.execute("INSERT OR IGNORE INTO dev_exits(buy_id,state,updated,reason) VALUES (?,'waiting',?,'dev-sell-100-percent')",
                                  (buy['id'],time.time()))
                    c.execute('UPDATE dev_sell_events SET state=?,reason=? WHERE signature=? AND buy_id=?',
                              ('triggered' if amount else 'ignored',str(amount),signature,buy['id']))
                    c.commit()
                if amount:
                    self.exit.next.pop(buy['id'],None);self.exit.wake.set()
                    self.notices.emit('dev sell detected',{'mint':buy['mint'],'source_signature':signature,
                        'amount':str(amount),'reason':'dev-sell-100-percent','mode':self.config.mode},alert=True,
                        key='dev-sell:'+buy['id'],interval=60)
            except asyncio.CancelledError:raise
            except Exception as exc:
                stopped=time.time()-event['first_seen']>=600
                with self.store.db('trading') as c:
                    c.execute('UPDATE dev_sell_events SET state=?,due=?,attempts=attempts+1,reason=? WHERE signature=? AND buy_id=?',
                        ('stopped' if stopped else 'pending',min(event['first_seen']+600,time.time()+min(30,2**min(event['attempts'],5))),
                         type(exc).__name__,signature,buy['id']))
                if stopped:self.notices.emit('dev sell check stopped',{'mint':buy['mint'],'source_signature':signature,
                    'reason':type(exc).__name__},alert=True,key='dev-sell-unavailable:'+buy['id'])

    async def run(self):
        while True:
            self.wake.clear()
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception as exc:self.notices.emit('dev sell worker deferred',{'type':type(exc).__name__},alert=True,key='dev-sell-worker')
            try:await asyncio.wait_for(self.wake.wait(),timeout=0.2)
            except asyncio.TimeoutError:pass
