"""Synchronous create quantity gate; no RPC and no pending buy permission."""
import json
import time
from features.strategy.create_amount import analyze,requests,AmountUnavailable
from features.strategy.dev_holdings import within_limit,token_count


POLICY='create-transaction-v1'


def permitted(store,route,limit):
    if store.rows('trading',"SELECT 1 FROM dev_sell_events e JOIN orders o ON o.id=e.buy_id "
                  "WHERE o.mint=? AND e.state='triggered'",(route.trade.mint,)):return False
    rows=store.rows('trading','SELECT * FROM token_dev_holdings WHERE mint=?',(route.trade.mint,))
    if not rows:return limit is None
    row=rows[0]
    if row['state']!='allowed':return False
    if limit is None:return True
    try:
        detail=json.loads(row['detail'])
        return (detail.get('policy')==POLICY and row['signature']==route.trade.signature
                and row['slot']==route.trade.slot and row['wallet']==route.trade.wallet
                and within_limit(detail,limit))
    except (ValueError,TypeError,KeyError):return False


class CreateDevGate:
    def __init__(self,config,store,notices):
        self.config,self.store,self.notices=config,store,notices
        self.changed=lambda:None

    def row(self,mint):
        rows=self.store.rows('trading','SELECT * FROM token_dev_holdings WHERE mint=?',(mint,))
        return rows[0] if rows else None

    def start_create(self,item,tx,route):
        limit=self.config.max_dev_holding_tokens
        previous=self.row(item.mint)
        if previous and previous['state'] in ('blocked','unavailable'):return False
        if limit is None:return not previous or previous['state']=='allowed'
        detail={'policy':POLICY,'limit_tokens':str(limit),'creator':item.user}
        try:
            buys=requests(item,tx)
            specified=sum(b['requested_raw'] for b in buys) if buys and all(b['requested_raw'] is not None for b in buys) else None
            fast={'amount_raw':str(specified),'decimals':item.decimals} if specified is not None else None
            if item.launchpad=='pump.fun' and fast and not within_limit(fast,limit):
                # Verified exact-output amount alone already proves rejection.
                # Do not demand a later balance/account response to reject it.
                detail.update(fast,quantity_source='calldata',calldata_requested_raw=str(specified),
                              metadata_creator=item.creator,buy_methods=[b['method'] for b in buys])
            else:
                detail.update(analyze(item,tx))
            allowed=within_limit(detail,limit)
            state='allowed' if allowed else 'blocked'
            detail.update(reason='dev-create-within-limit' if allowed else 'dev-create-over-limit',
                          holding_tokens=token_count(int(detail['amount_raw']),detail['decimals']))
        except (AmountUnavailable,ValueError,TypeError,KeyError,IndexError,AttributeError) as exc:
            state='unavailable'
            detail['reason']=str(exc) if isinstance(exc,AmountUnavailable) else 'dev-create-evidence-invalid'
        with self.store.db('trading') as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT * FROM token_dev_holdings WHERE mint=?',(item.mint,)).fetchone()
            if current and (current['state'] in ('blocked','unavailable')
                    or current['signature']!=item.signature or current['slot']!=item.slot):
                db.commit();return False
            db.execute('INSERT OR REPLACE INTO token_dev_holdings VALUES (?,?,?,?,?,?,?)',
                (item.mint,item.signature,item.user,item.slot,time.time(),state,json.dumps(detail)))
            db.commit()
        # An over-limit create is a routine rejection: retain its local log/audit.
        self.notices.emit('dev create quantity check',{'mint':item.mint,'state':state,**detail},alert=state=='unavailable')
        self.changed()
        return state=='allowed'

    def recover(self):
        # Existing signed orders keep their reconciliation/emergency-exit state.
        # Legacy unfinished checks cannot certify a new create entry.
        with self.store.db('trading') as db:
            db.execute("UPDATE token_dev_holdings SET state='unavailable' WHERE state='checking'")
            for row in db.execute("SELECT * FROM token_dev_holdings WHERE state='allowed'").fetchall():
                try:
                    detail=json.loads(row['detail']);raw=detail['amount_raw']
                    if not isinstance(raw,str) or not raw.isascii() or not raw.isdecimal():
                        raise ValueError('invalid-saved-quantity')
                    over=not within_limit(detail,self.config.max_dev_holding_tokens)
                except (ValueError,KeyError,TypeError):
                    # Unknown evidence is not proof of excess and must not arm
                    # a legacy emergency exit against an existing position.
                    db.execute("UPDATE token_dev_holdings SET state='unavailable' WHERE mint=?",(row['mint'],))
                    continue
                if over:db.execute("UPDATE token_dev_holdings SET state='blocked' WHERE mint=?",(row['mint'],))

    async def close(self):
        pass  # No asynchronous checks/tasks are launched.
