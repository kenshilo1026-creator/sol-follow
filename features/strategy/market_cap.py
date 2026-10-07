"""Freeze each mint's first eligible observation; exclusions survive restarts."""
import asyncio
import json
import time
from decimal import Decimal
from trade_execution.builder import BuildError


def usd(micros):return format(Decimal(micros)/10**6,'f')


class MarketCapGate:
    def __init__(self,config,store,notices,builder):
        self.config,self.store,self.notices,self.builder=config,store,notices,builder
        self.lock=asyncio.Lock()

    def row(self,mint):
        rows=self.store.rows('trading','SELECT * FROM token_entry_caps WHERE mint=?',(mint,))
        return rows[0] if rows else None

    async def check(self,route):
        trade=route.trade;limit=self.config.max_market_cap_usd_micros
        # Only one first observation can win, even when workers overlap.
        async with self.lock:
            previous=self.row(trade.mint)
            if previous:
                if previous['state']!='allowed':return False
                if not limit or int(previous['cap_usd_micros'])<=limit:return True
                self.finish(trade.mint,'blocked',previous['cap_usd_micros'],limit,
                            {'reason':'saved-first-market-cap-over-new-limit'})
                return False
            if not limit:return True
            with self.store.db('trading') as c:
                c.execute("INSERT INTO token_entry_caps VALUES (?,?,?,?,?,'checking','0',?,'{}')",
                          (trade.mint,trade.signature,trade.wallet,trade.slot,time.time(),str(limit)))
                c.execute('DELETE FROM votes WHERE mint=?',(trade.mint,))
            try:
                result=await self.builder.market_cap(route)
                cap=result.get('marketCapUsdMicros')
                if not isinstance(cap,str) or not cap.isdecimal() or len(cap)>80 or int(cap)<=0:
                    raise BuildError('market-cap-price-unavailable')
                if type(result.get('snapshotSlot')) is not int or result['snapshotSlot']<trade.slot:
                    raise BuildError('cache-slot-behind')
                state='blocked' if int(cap)>limit else 'allowed'
                self.finish(trade.mint,state,cap,limit,result)
                return state=='allowed'
            except asyncio.CancelledError:
                # A persisted checking row stays closed after a restart: never
                # substitute a later buyer's price for the lost first snapshot.
                raise
            except Exception as exc:
                reason=str(exc) if isinstance(exc,BuildError) else type(exc).__name__
                self.finish(trade.mint,'unavailable','0',limit,{'reason':reason})
                return False

    def finish(self,mint,state,cap,limit,detail):
        with self.store.db('trading') as c:
            c.execute('UPDATE token_entry_caps SET state=?,cap_usd_micros=?,threshold=?,detail=? WHERE mint=?',
                      (state,str(cap),str(limit),json.dumps(detail),mint))
            if state!='allowed':c.execute('DELETE FROM votes WHERE mint=?',(mint,))
        self.notices.emit('market cap entry check',{'mint':mint,'state':state,
            'market_cap_usd':usd(cap) if int(cap)>0 else None,'limit_usd':usd(limit),**detail},alert=state!='allowed')


def permitted(store,mint,limit):
    rows=store.rows('trading','SELECT state,cap_usd_micros FROM token_entry_caps WHERE mint=?',(mint,))
    if not rows:return not limit
    row=rows[0]
    return row['state']=='allowed' and (not limit or int(row['cap_usd_micros'])<=limit)


def reset(store,mint):
    with store.db('trading') as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM orders WHERE mint=? AND state IN ('reserved','signed','submitted','unknown','confirmed')",
                     (mint,)).fetchone():
            raise ValueError('market-cap-reset-has-active-order')
        c.execute('DELETE FROM votes WHERE mint=?',(mint,))
        count=c.execute('DELETE FROM token_entry_caps WHERE mint=?',(mint,)).rowcount
        c.commit()
    return bool(count)


def main():
    import argparse
    from share_common.config import load
    from share_common.instance import Instance
    from features.database.storage import Store
    from chain_common.primitives import pubkey
    parser=argparse.ArgumentParser(description='Inspect/reset persistent first-observation market-cap decisions.')
    parser.add_argument('command',choices=('list','reset'))
    parser.add_argument('--mint',type=lambda s:str(pubkey(s)))
    args=parser.parse_args()
    if args.command=='reset' and not args.mint:parser.error('reset requires --mint')
    config=load()
    if args.command=='list':
        store=Store(config.data)
        rows=store.rows('trading','SELECT mint,state,cap_usd_micros,threshold,signature,slot FROM token_entry_caps'+
                        (' WHERE mint=?' if args.mint else ''),(args.mint,) if args.mint else ())
        print(json.dumps(rows,indent=2))
        return
    # Prevent resetting a mint while a running service is using its old votes.
    instance=Instance(config.data)
    try:
        print(json.dumps({'mint':args.mint,'reset':reset(Store(config.data),args.mint)}))
    finally:instance.close()


if __name__=='__main__':main()
