"""Two background RPC reads; pending checks permit buys, exclusions persist."""
import asyncio
import json
import time
from fractions import Fraction
from chain_common.primitives import SYSTEM, WSOL, pubkey
from chain_common.accounts import account_data, key
from launchpads import pump_fun, stonk


class HoldingError(ValueError):
    pass


def within_limit(detail,limit):
    # Compare raw units exactly, including fractional-token boundaries.
    return limit is None or Fraction(int(detail['amount_raw']),10**detail['decimals'])<=Fraction(limit)


def row_permitted(row,limit):
    if row is None:return limit is None
    if row['state']=='checking':
        return time.time()-row['observed']<30
    if row['state']!='allowed':return False
    try:return within_limit(json.loads(row['detail']),limit)
    except (ValueError,KeyError,TypeError):return False


def permitted(store,mint,limit):
    rows=store.rows('trading','SELECT * FROM token_dev_holdings WHERE mint=?',(mint,))
    return row_permitted(rows[0] if rows else None,limit)


def token_count(amount,decimals):
    if not decimals:return str(amount)
    digits=str(amount).zfill(decimals+1)
    return (digits[:-decimals]+'.'+digits[-decimals:]).rstrip('0').rstrip('.')


class DevHoldingsGate:
    def __init__(self,config,store,notices):
        self.config,self.store,self.notices=config,store,notices
        self.tasks={}
        self.changed=lambda:None

    def row(self,mint):
        rows=self.store.rows('trading','SELECT * FROM token_dev_holdings WHERE mint=?',(mint,))
        return rows[0] if rows else None

    async def creator(self,route,rpc):
        result=await rpc.call('getMultipleAccounts',[[route.trade.mint,route.trade.pool],
            {'encoding':'base64','commitment':self.config.hotlist_commitment,'minContextSlot':route.trade.slot}])
        slot=result['context']['slot']
        if type(slot) is not int or slot<route.trade.slot:raise HoldingError('dev-snapshot-stale')
        mint,pool=result['value']
        raw_mint=account_data(mint,route.token_program)
        if len(raw_mint)<82 or raw_mint[45]!=1:raise HoldingError('dev-mint-invalid')
        if route.kind in ('pump_native_curve','sol_to_pump_curve','meteora_dlmm_to_pump_curve'):
            if route.trade.pool!=pump_fun.curve_address(route.trade.mint) or not pump_fun.verify(pool):
                raise HoldingError('dev-pool-invalid')
            raw=account_data(pool,pump_fun.PROGRAM)
            if len(raw)<81:raise HoldingError('dev-creator-missing')
            quote=key(raw,83) if len(raw)>=115 else SYSTEM
            if (WSOL if quote==SYSTEM else quote)!=route.quote_mint:raise HoldingError('dev-quote-mismatch')
            creator=key(raw,49)
        elif route.kind in ('stonk_native_curve','sol_to_stonk_curve'):
            if not stonk.verify_pool(route.trade.pool,pool,route.trade.mint):raise HoldingError('dev-pool-invalid')
            raw=account_data(pool,stonk.PROGRAM)
            if key(raw,237)!=route.quote_mint or raw[18]!=raw_mint[44]:raise HoldingError('dev-pool-invalid')
            creator=key(raw,333)
        else:raise HoldingError('dev-route-unsupported')
        if creator==SYSTEM:raise HoldingError('dev-creator-missing')
        return creator,raw_mint[44],slot

    def holdings(self,result,route,creator,decimals):
        slot=result['context']['slot']
        if type(slot) is not int or slot<route.trade.slot:raise HoldingError('dev-snapshot-stale')
        rows=result['value']
        if not isinstance(rows,list):raise HoldingError('dev-accounts-invalid')
        total=0;seen=set()
        for row in rows:
            key=row['pubkey'];pubkey(key)
            if key in seen:raise HoldingError('dev-account-duplicate')
            seen.add(key)
            account=row['account'];parsed=account['data']['parsed'];info=parsed['info']
            amount=info['tokenAmount'];raw=amount['amount']
            if (account['owner']!=route.token_program or account.get('executable') is not False
                    or parsed['type']!='account' or info['owner']!=creator or info['mint']!=route.trade.mint
                    or info['state'] not in ('initialized','frozen')
                    or type(amount['decimals']) is not int or amount['decimals']!=decimals
                    or not isinstance(raw,str) or not raw.isascii() or not raw.isdecimal()
                    or len(raw)>20 or int(raw)>2**64-1):
                raise HoldingError('dev-account-invalid')
            total+=int(raw)
        return {'amount_raw':str(total),'decimals':decimals,'holding_tokens':token_count(total,decimals),
                'snapshot_slot':slot,'account_count':len(rows)}

    def start(self,route,rpc):
        """Persist ownership synchronously, then let signal processing continue."""
        trade=route.trade;limit=self.config.max_dev_holding_tokens
        previous=self.row(trade.mint)
        if previous:
            if previous['state']=='allowed' and not row_permitted(previous,limit):
                self.finish(trade.mint,'blocked',{**json.loads(previous['detail']),
                    'limit_tokens':str(limit),'reason':'saved-dev-holdings-over-new-limit'})
                return False
            return row_permitted(previous,limit)
        if limit is None:return True
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL')
            inserted=c.execute("INSERT OR IGNORE INTO token_dev_holdings VALUES (?,?,?,?,?,'checking',?)",
                (trade.mint,trade.signature,trade.wallet,trade.slot,time.time(),json.dumps({'limit_tokens':str(limit)}))).rowcount
        if inserted:
            task=asyncio.create_task(self.query(route,rpc))
            self.tasks[trade.mint]=task
            task.add_done_callback(lambda task:self.completed(trade.mint,task))
        return row_permitted(self.row(trade.mint),limit)

    def completed(self,mint,task):
        self.tasks.pop(mint,None)
        if not task.cancelled() and task.exception() is not None:
            self.notices.emit('dev holding persistence failed',{'mint':mint,'type':type(task.exception()).__name__},
                              alert=True,key='dev-persist:'+mint)

    async def check(self,route,rpc):
        self.start(route,rpc)
        if route.trade.mint in self.tasks:await self.tasks[route.trade.mint]
        return permitted(self.store,route.trade.mint,self.config.max_dev_holding_tokens)

    async def query(self,route,rpc):
        trade=route.trade;limit=self.config.max_dev_holding_tokens
        detail={'limit_tokens':str(limit),'creator_source':'on-chain-pool'}
        try:
            remaining=max(0,30-(time.time()-self.row(trade.mint)['observed']))
            async with asyncio.timeout(remaining):
                creator,decimals,slot=await self.creator(route,rpc)
                detail.update(creator=creator,creator_slot=slot)
                result=await rpc.call('getTokenAccountsByOwner',[creator,{'mint':trade.mint},
                    {'encoding':'jsonParsed','commitment':self.config.hotlist_commitment,'minContextSlot':slot}])
                if result['context']['slot']<slot:raise HoldingError('dev-snapshot-stale')
                detail.update(self.holdings(result,route,creator,decimals))
                allowed=within_limit(detail,limit)
                detail['reason']='dev-holdings-within-limit' if allowed else 'dev-holdings-over-limit'
            self.finish(trade.mint,'allowed' if allowed else 'blocked',detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail['reason']=str(exc) if isinstance(exc,HoldingError) else type(exc).__name__
            self.finish(trade.mint,'unavailable',detail)

    def recover(self):
        for row in self.store.rows('trading',"SELECT * FROM token_dev_holdings WHERE state='checking'"):
            self.finish(row['mint'],'unavailable',{**json.loads(row['detail']),'reason':'check-interrupted'})
        for row in self.store.rows('trading',"SELECT * FROM token_dev_holdings WHERE state='allowed'"):
            if not row_permitted(row,self.config.max_dev_holding_tokens):
                self.finish(row['mint'],'blocked',{**json.loads(row['detail']),
                    'limit_tokens':str(self.config.max_dev_holding_tokens),'reason':'saved-dev-holdings-over-new-limit'})

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    def finish(self,mint,state,detail):
        with self.store.db('trading') as c:
            c.execute('PRAGMA synchronous=FULL')
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE token_dev_holdings SET state=?,detail=? WHERE mint=?',
                      (state,json.dumps(detail),mint))
            if state!='allowed':c.execute('DELETE FROM votes WHERE mint=?',(mint,))
            c.commit()
        self.notices.emit('dev holding entry check',{'mint':mint,'state':state,**detail},alert=state!='allowed')
        self.changed()
