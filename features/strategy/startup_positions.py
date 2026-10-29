"""Retire externally emptied positions at startup and before live dev exits.

RPC contract: https://solana.com/docs/rpc/http/gettokenaccountsbyowner
Only an explicit finalized zero across every token account closes a position.
"""
import asyncio
import base64
import time
from solders.transaction import VersionedTransaction
from chain_common.primitives import TOKEN,TOKEN_2022,pubkey

ACTIVE="('reserved','signed','submitted','unknown','confirmed')"
REASON='startup-wallet-balance-zero'


def total_balance(result,wallet,mint,floor):
    slot=result['context']['slot']
    if type(slot) is not int or slot<floor:raise ValueError('balance-context-behind-buy')
    rows=result['value']
    if not isinstance(rows,list):raise ValueError('balance-list-invalid')
    total=0;seen=set()
    for row in rows:
        address=str(pubkey(row['pubkey']))
        account=row['account'];parsed=account['data']['parsed'];info=parsed['info']
        amount=info['tokenAmount']['amount']
        if (address in seen or account['owner'] not in (TOKEN,TOKEN_2022)
            or account.get('executable') is not False or parsed['type']!='account'
            or info['owner']!=wallet or info['mint']!=mint
            or not isinstance(amount,str) or not amount.isascii() or not amount.isdigit()
            or len(amount)>20 or int(amount)>=2**64):
            raise ValueError('balance-account-invalid')
        seen.add(address);total+=int(amount)
    return total,slot


def pending(c,mint,mode):
    return bool(c.execute(f"SELECT 1 FROM orders WHERE mint=? AND mode=? AND state IN {ACTIVE}",
                         (mint,mode)).fetchone() or c.execute(
        "SELECT 1 FROM dev_exits e JOIN orders o ON o.id=e.buy_id WHERE o.mint=? AND o.mode=? "
        "AND e.state NOT IN ('finalized','cancelled','dry-skipped') AND (e.signature IS NOT NULL OR e.raw IS NOT NULL)",
        (mint,mode)).fetchone())


async def prune_empty_position(config,store,rpc,notices,pos,*,reason=REASON,balance_first=False):
    mint,mode=pos['mint'],pos['mode']
    with store.db('trading') as c:
        if pending(c,mint,mode):return False
        buy=c.execute("SELECT * FROM orders WHERE mint=? AND mode=? AND side='buy' AND state='finalized' "
                      "ORDER BY created DESC,id DESC LIMIT 1",(mint,mode)).fetchone()
        if not buy:return False
        buy=dict(buy)
    # The database is reusable with another configured wallet. Never close
    # its holdings using the new wallet's zero balance.
    signed=VersionedTransaction.from_bytes(base64.b64decode(buy['raw'],validate=True))
    if (str(signed.message.account_keys[0])!=config.wallet_address
        or str(signed.signatures[0])!=buy['signature']):return False
    result=None
    if balance_first:
        # A positive balance needs only one read on the live exit path. A zero
        # is not trusted until its context is checked against the finalized buy.
        result=await rpc.call('getTokenAccountsByOwner',[config.wallet_address,{'mint':mint},
            {'encoding':'jsonParsed','commitment':'finalized','minContextSlot':0}])
        amount,_=total_balance(result,config.wallet_address,mint,0)
        if amount:return False
    status=await rpc.call('getSignatureStatuses',[[buy['signature']],{'searchTransactionHistory':True}])
    values=status['value']
    if not isinstance(values,list) or len(values)!=1:return False
    status=values[0]
    if (not isinstance(status,dict) or status.get('confirmationStatus')!='finalized'
        or 'err' not in status or status['err'] is not None
        or type(status.get('slot')) is not int or status['slot']<0):return False
    if result is None:
        result=await rpc.call('getTokenAccountsByOwner',[config.wallet_address,{'mint':mint},
            {'encoding':'jsonParsed','commitment':'finalized','minContextSlot':status['slot']}])
    amount,slot=total_balance(result,config.wallet_address,mint,status['slot'])
    if amount:return False
    with store.db('trading') as c:
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT amount,updated FROM positions WHERE mint=? AND mode=?',(mint,mode)).fetchone()
        if (not current or current['amount']!=pos['amount'] or current['updated']!=pos['updated']
            or pending(c,mint,mode)):
            c.commit();return False
        # Keep fills, entry cost and dedup evidence; zero is the existing
        # terminal holding convention used by take-profit and dev exits.
        c.execute("UPDATE positions SET amount='0',updated=? WHERE mint=? AND mode=?",(time.time(),mint,mode))
        ids="SELECT id FROM orders WHERE mint=? AND mode=? AND side='buy' AND state='finalized'"
        c.execute("UPDATE dev_exits SET state='cancelled',updated=?,reason=? WHERE buy_id IN ("+ids+") "
                  "AND signature IS NULL AND raw IS NULL AND state NOT IN ('finalized','cancelled','dry-skipped')",
                  (time.time(),reason,mint,mode))
        c.execute('DELETE FROM dev_sell_watches WHERE buy_id IN ('+ids+')',(mint,mode))
        c.execute("UPDATE dev_sell_events SET state='ignored',reason=? WHERE state='pending' AND buy_id IN ("+ids+')',
                  (reason,mint,mode))
        c.commit()
    notices.emit('position closed after startup balance check' if reason==REASON else 'position closed after wallet balance check',
                 {'mint':mint,'wallet':config.wallet_address,'balance':'0','slot':slot,'reason':reason})
    return True

async def prune_empty_positions(config,store,rpc,notices):
    if config.dry_run or not config.wallet_address:return 0
    positions=store.rows('trading',"SELECT * FROM positions WHERE mode=? AND amount!='0'",(config.mode,))
    limit=asyncio.Semaphore(2)

    async def bounded(pos):
        async with limit:
            try:return await asyncio.wait_for(prune_empty_position(config,store,rpc,notices,pos),timeout=20)
            except asyncio.CancelledError:raise
            except Exception as exc:
                notices.emit('startup position balance check deferred',{'mint':pos['mint'],'type':type(exc).__name__})
                return False
    return sum(await asyncio.gather(*(bounded(pos) for pos in positions)))


async def retire_empty_position(config,store,rpc,notices,mint):
    """Event-driven live cleanup; failed/old RPC evidence never means zero.

    Reuse the startup ownership, finalized-buy and atomic position checks.
    No polling and no cached balance: manual sales can invalidate the SDK cache.
    """
    if config.dry_run or not config.wallet_address:return False
    with store.db('trading') as c:
        if pending(c,mint,config.mode):return False
        pos=c.execute('SELECT * FROM positions WHERE mint=? AND mode=?',(mint,config.mode)).fetchone()
        if not pos:return False
        pos=dict(pos)
    if pos['amount']=='0':return True
    try:
        return await asyncio.wait_for(prune_empty_position(config,store,rpc,notices,pos,
            reason='wallet-balance-zero',balance_first=True),timeout=2)
    except asyncio.CancelledError:raise
    except Exception as exc:
        notices.emit('position balance check deferred',{'mint':mint,'type':type(exc).__name__})
        return False
