"""Always-on confirmed create safety net, independent of the hotlist gRPC.

Only current hotlist logs are watched. No historical replay becomes a buy:
notification receipt, matching confirmed HTTP receipt and chain time are all
required. Both sources converge on the same durable inbox and order dedupe.
"""
import asyncio
from dataclasses import replace
import json
import time

from chain_common.transaction import Tx
from chain_common.availability import Availability
from features.audit.feed_trace import observed
from features.funding.discovery import Discovery
from launchpads.create import inspect

CREATE_LOGS = {'Program log: Instruction: '+name for name in (
    'Create', 'CreateV2', 'Initialize', 'InitializeV2', 'InitializeWithToken2022')}
BACKUP_REASON = 'create-backup-confirmed'


class CreateFeed(Discovery):
    def __init__(self, config, store, rpc, notices):
        super().__init__(replace(config,cex={},privacy_pools=()),store,rpc,notices)
        self.ws_urls=tuple(dict.fromkeys([config.ws,config.funding_ws_url]))
        self.feed_health=Availability(notices,'create-backup')
        self.failure_message='create backup unavailable; reconnecting'
        self.pending={}
        self.seen={}
        self.wake=asyncio.Event()

    def notification(self, body, subs):
        params=body.get('params',{});result=params.get('result',{});value=result.get('value',{})
        sig=value.get('signature');slot=result.get('context',{}).get('slot')
        address=next((a for a,s in subs.items() if s==params.get('subscription')),None)
        if (address not in self.store.hotlist(time.time()) or value.get('err') is not None
                or not isinstance(sig,str) or not sig or type(slot) is not int or slot<=0
                or not CREATE_LOGS.intersection(value.get('logs') or [])):
            return
        now=time.time()
        self.seen={s:t for s,t in self.seen.items() if now-t<=max(60,self.config.signal_age)}
        if sig in self.seen:return
        if len(self.pending)>=2048:
            self.notices.emit('create backup overloaded',{},key='create-backup-overload',interval=60)
            return
        observed(self.store,sig,'create-confirmed','received',slot,now)
        self.seen[sig]=now
        self.pending[sig]={'slot':slot,'seen':now,'due':now,'address':address}
        self.received+=1
        self.wake.set()

    async def once(self):
        for sig,event in list(self.pending.items()):
            now=time.time()
            if now-event['seen']>self.config.signal_age:
                self.pending.pop(sig,None);continue
            if event['due']>now:continue
            try:
                fetch_started=time.monotonic()
                raw=await self.rpc.transaction(sig,commitment='confirmed')
                if raw is None:raise ValueError('create-backup-not-indexed')
                tx=Tx(raw)
                if tx.signature!=sig or tx.slot!=event['slot']:
                    raise ValueError('create-backup-receipt-mismatch')
                now=time.time()
                hotlist=self.store.hotlist(now)
                creates=inspect(tx).creates
                eligible=any(c.user==event['address'] and c.user in tx.signers and c.user in hotlist for c in creates)
                if eligible and 0<=now-event['seen']<=self.config.signal_age and 0<=now-tx.time<=self.config.signal_age:
                    observed(self.store,sig,'create-confirmed','verified',tx.slot,now,
                             receipt_ms=round((time.monotonic()-fetch_started)*1000,3),
                             since_log_ms=round((now-event['seen'])*1000,3))
                    self.commit(raw,event)
                self.pending.pop(sig,None)
            except asyncio.CancelledError:raise
            except Exception as exc:
                event['due']=time.time()+0.5
                self.notices.emit('create backup receipt deferred',{'type':type(exc).__name__},
                                  key='create-backup-receipt',interval=60)
            return

    def commit(self, raw, event):
        sig=raw['transaction']['signatures'][0];now=time.time()
        raw={**raw,'_stream':{'commitment':'confirmed','live':True,'source':'create-backup'}}
        with self.store.db('funding') as db:
            db.execute('BEGIN IMMEDIATE')
            proof=db.execute('SELECT * FROM processed_signals WHERE signature=?',(sig,)).fetchone()
            dead=db.execute('SELECT 1 FROM dead_slots WHERE slot=?',(raw['slot'],)).fetchone()
            if dead or proof and (proof['state']=='invalid' or proof['slot']!=raw['slot']):
                return
            # This is confirmed evidence, never fabricated processed evidence.
            db.execute('INSERT INTO processed_signals VALUES (?,?,?,?,?,?,?) ON CONFLICT(signature) DO UPDATE SET '
                       "state='confirmed',live=1,seen=excluded.seen,reason=excluded.reason,due=excluded.due",
                       (sig,raw['slot'],event['seen'],1,'confirmed',now+5,BACKUP_REASON))
            db.execute("INSERT INTO jobs(signature,state,first_seen,due,slot) VALUES (?,'pending',?,?,?) "
                       "ON CONFLICT(signature) DO UPDATE SET state='pending',due=excluded.due,reason='create-backup'",
                       (sig,event['seen'],now,raw['slot']))
            db.execute('INSERT OR REPLACE INTO stream_payloads VALUES (?,?)',(sig,json.dumps(raw)))
            db.commit()
        self.store.mark_live(sig)
        self.added+=1

    async def receipts(self):
        while True:
            self.wake.clear()
            await self.once()
            try:await asyncio.wait_for(self.wake.wait(),timeout=0.05)
            except asyncio.TimeoutError:pass

    async def run(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self.websocket())
            group.create_task(self.receipts())
