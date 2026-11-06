"""One-shot post-confirmation popularity exit; never on the buy critical path."""
import asyncio
import base64
import json
import time
from chain_common.primitives import TOKEN, TOKEN_2022, pubkey, b58encode
from features.audit.writer import record
from features.strategy.holder_diagnostics import HolderUnavailable, failure


def holder_count(result, mint, program, wallet, min_slot):
    """Count distinct positive-balance owners, including dev, us and pool PDAs."""
    if program not in (TOKEN, TOKEN_2022):raise ValueError('holder-token-program')
    slot=result.get('context',{}).get('slot') if isinstance(result,dict) else None
    if type(slot) is not int or slot<min_slot:raise ValueError('holder-context-behind')
    rows=result.get('value')
    if not isinstance(rows,list) or len(rows)>=10000:raise ValueError('holder-response-incomplete')
    owners=set();accounts=set();mint_bytes=bytes(pubkey(mint))
    for item in rows:
        address=str(pubkey(item['pubkey']))
        if address in accounts:raise ValueError('holder-duplicate-account')
        accounts.add(address)
        account=item['account'];data=account['data']
        if account.get('owner')!=program or account.get('executable') is not False:
            raise ValueError('holder-account-owner')
        if not isinstance(data,list) or len(data)!=2 or data[1]!='base64':raise ValueError('holder-account-encoding')
        raw=base64.b64decode(data[0],validate=True)
        if len(raw)!=165 or raw[:32]!=mint_bytes or raw[108] not in (1,2):
            raise ValueError('holder-account-layout')
        if int.from_bytes(raw[64:72],'little')>0:owners.add(b58encode(raw[32:64]))
    # A successful buy must be visible. Empty/lagging indexes are not zero holders.
    if wallet not in owners:raise ValueError('holder-buy-not-visible')
    return len(owners),slot


class HolderCheck:
    def __init__(self,config,store,public,alchemy,notices,priority,exit_engine):
        self.config,self.store,self.public,self.alchemy=config,store,public,alchemy
        self.notices,self.priority,self.exit=notices,priority,exit_engine
        self.wake=asyncio.Event()

    def arm(self,buy,slot):
        if self.config.dry_run or not self.config.holder_check_enabled:return
        if buy['side']!='buy' or buy['mode']!=self.config.mode or not buy['signature']:return
        if type(slot) is not int or slot<0:return
        now=time.time()
        with self.store.db('trading') as c:
            c.execute("INSERT OR IGNORE INTO holder_checks(buy_id,due,min_slot,minimum,state,updated) "
                      "VALUES (?,?,?,?,'waiting',?)",(buy['id'],now+self.config.holder_check_seconds,
                      slot,self.config.min_holders,now))
        self.wake.set()

    def eligible(self,job):
        rows=self.store.rows('trading',"SELECT o.*,r.request FROM orders o JOIN order_routes r ON r.order_id=o.id "
            "WHERE o.id=? AND o.mode=? AND o.side='buy' AND o.state IN ('confirmed','finalized')",
            (job['buy_id'],self.config.mode))
        if not rows:return None
        buy=rows[0]
        if self.store.rows('funding','SELECT 1 FROM dead_slots WHERE slot=?',(job['min_slot'],)):return None
        pos=self.store.rows('trading','SELECT amount FROM positions WHERE mint=? AND mode=?',(buy['mint'],buy['mode']))
        if pos and int(pos[0]['amount'])<=0:return None
        if self.store.rows('trading','SELECT 1 FROM dev_exits WHERE buy_id=?',(buy['id'],)):return None
        return buy

    async def count(self,buy,job):
        request=json.loads(buy['request']);program=request['tokenProgram']
        if program not in (TOKEN,TOKEN_2022):raise ValueError('holder-token-program')
        filters=[{'memcmp':{'offset':0,'bytes':buy['mint']}}]
        if program==TOKEN:filters.append({'dataSize':165})
        params=[program,{'commitment':'confirmed','withContext':True,'minContextSlot':job['min_slot'],
                        'encoding':'base64','dataSlice':{'offset':0,'length':165},'filters':filters}]
        failures=[]
        for provider,client in [('public',self.public),('alchemy',self.alchemy)]:
            if client is None:
                failures.append({'provider':provider,'reason':'holder-provider-not-configured'})
                continue
            began=time.monotonic();phase='rpc'
            try:
                result=await asyncio.wait_for(client.call('getProgramAccounts',params),4)
                phase='validation'
                count,slot=holder_count(result,buy['mint'],program,self.config.wallet_address,job['min_slot'])
                return count,slot,provider
            except asyncio.CancelledError:raise
            except Exception as exc:
                failures.append(failure(exc,provider,(time.monotonic()-began)*1000,phase))
        raise HolderUnavailable(failures)

    async def check(self,job):
        buy=self.eligible(job)
        if not buy:
            with self.store.db('trading') as c:c.execute("UPDATE holder_checks SET state='skipped',updated=? WHERE buy_id=?",(time.time(),job['buy_id']))
            return
        detail={'order':buy['id'],'minimum':job['minimum'],'due':job['due'],'checked_at':time.time()}
        try:
            count,slot,provider=await self.count(buy,job)
            detail.update(holders=count,slot=slot,provider=provider)
            state='passed' if count>=job['minimum'] else 'triggered'
        except asyncio.CancelledError:raise
        except HolderUnavailable as exc:
            state='unavailable';detail['failures']=exc.failures
        except Exception:
            state='unavailable';detail['check_error']='holder-check-error'
        # Recheck after network awaits: dev sell/manual sell may already have won.
        if not self.eligible(job):state='skipped'
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state FROM holder_checks WHERE buy_id=?',(buy['id'],)).fetchone()
            if not current or current['state']!='waiting':return
            if state=='triggered':
                inserted=c.execute("INSERT OR IGNORE INTO dev_exits(buy_id,state,updated,reason) "
                    "VALUES (?,'waiting',?,'holder-check')",(buy['id'],time.time())).rowcount
                if not inserted:state='skipped'
            c.execute('UPDATE holder_checks SET state=?,updated=?,detail=? WHERE buy_id=?',
                      (state,time.time(),json.dumps(detail),buy['id']))
            c.commit()
        if state=='triggered':self.exit.next.pop(buy['id'],None);self.exit.wake.set()
        origins=self.store.rows('trading','SELECT wallet FROM order_wallets WHERE order_id=? LIMIT 1',(buy['id'],))
        record(self.store,'holder-check',state,'holder-check-'+state,mint=buy['mint'],
               signature=buy['signature'],wallet=origins[0]['wallet'] if origins else '',
               event=buy['id']+':holders',detail=detail)
        if state=='unavailable':
            self.notices.emit('holder check unavailable',{'mint':buy['mint'],'mode':buy['mode'],**detail},
                              alert=True,key='holder-check:'+buy['id'])

    async def once(self):
        if not self.config.holder_check_enabled or self.config.dry_run or self.priority.active:return
        jobs=self.store.rows('trading',"SELECT h.* FROM holder_checks h JOIN orders o ON o.id=h.buy_id "
            "WHERE h.state='waiting' AND h.due<=? AND o.mode=? ORDER BY h.due LIMIT 16",(time.time(),self.config.mode))
        for job in jobs:
            if self.priority.active:return
            await self.check(job)

    async def run(self):
        while True:
            self.wake.clear()
            try:await self.once()
            except asyncio.CancelledError:raise
            except Exception:
                self.notices.emit('holder check worker deferred',{},key='holder-worker')
            try:await asyncio.wait_for(self.wake.wait(),timeout=0.25)
            except asyncio.TimeoutError:pass
