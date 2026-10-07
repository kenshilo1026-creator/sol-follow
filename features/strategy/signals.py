"""Distinct-wallet rolling votes, persistent dedupe and atomic order reservation."""
import time
from chain_common.primitives import ata, TOKEN, TOKEN_2022
from features.funding.processed import Processed
from features.strategy.market_cap import permitted


class Signals:
    def __init__(self, store, config, started=None):
        self.store, self.config = store, config
        self.started = time.time() if started is None else started
        self.proofs = Processed(store)
        self.votes = {}
        for row in store.rows('trading','SELECT * FROM votes WHERE time>=?',(time.time()-config.window,)):
            self.votes.setdefault(row['mint'],{})[row['wallet']] = row

    def observe(self, trade, now=None):
        now = time.time() if now is None else now
        cfg = self.config
        if not self.store.eligible(trade.wallet, trade.slot, trade.time, now):
            return None
        processed=cfg.hotlist_commitment=='processed'
        proof=self.proofs.row(trade.signature) if processed else None
        if processed and (not proof or proof['state']!='pending'):
            return None
        if not self.proofs.usable(trade.signature, now, trade.slot,require_processed=processed):
            return None
        if not permitted(self.store,trade.mint,cfg.max_market_cap_usd_micros):
            return None
        fresh = self.store.vote(trade)
        current=self.store.rows('trading','SELECT * FROM votes WHERE mint=? AND wallet=?',(trade.mint,trade.wallet))
        if current:
            self.votes.setdefault(trade.mint,{})[trade.wallet]=current[0]
        if not fresh or trade.side != 'buy' or trade.time < max(self.started, now-cfg.signal_age):
            return None
        if not current or current[0]['signature']!=trade.signature or current[0]['slot']!=trade.slot:
            return None
        rows = [r for r in self.votes.get(trade.mint,{}).values() if r['time']>=now-cfg.window]
        selected=[]
        for r in rows:
            if cfg.remaining and int(r['amount'])<=0:
                continue
            if self.proofs.usable(r['signature'],now,r['slot'],require_processed=processed) and self.store.eligible(r['wallet'],r['slot'],r['time'],now):
                selected.append(r)
                if len(selected)>=cfg.n:
                    return self.store.reserve(cfg, trade.mint, trade.pool, cfg.buy_amount,sources=selected)
        return None

    def reject_mint(self,mint):
        with self.store.db('trading') as c:c.execute('DELETE FROM votes WHERE mint=?',(mint,))
        self.votes.pop(mint,None)

    def reject(self, trade):
        self.store.reject_vote(trade)
        current=self.votes.get(trade.mint,{}).get(trade.wallet)
        if current and current['slot']<=trade.slot:
            self.votes[trade.mint].pop(trade.wallet,None)

    def prune(self, now=None):
        now=time.time() if now is None else now
        self.votes={mint:{wallet:r for wallet,r in rows.items() if r['time']>=now-self.config.window}
                    for mint,rows in self.votes.items()}
        self.votes={mint:rows for mint,rows in self.votes.items() if rows}

    def invalidate(self, signature):
        for rows in self.votes.values():
            for wallet in [w for w,r in rows.items() if r['signature']==signature]:
                rows.pop(wallet)

    def observe_holdings(self, tx):
        """A transfer/unsupported route may empty the counted ATA without being
        a supported sell. Revoke that vote; never turn a transfer into a buy.
        Missing owner metadata conservatively revokes a touched counted ATA.
        """
        post=tx.tokens('post')
        keys=set(tx.keys)
        touched={(owner,mint) for owner,mint,_ in [*tx.tokens('pre').values(),*post.values()]}
        for wallet,mint in touched:
            row=self.votes.get(mint,{}).get(wallet)
            if row is None:
                continue
            accounts=[ata(wallet,mint,program) for program in (TOKEN,TOKEN_2022)]
            if not any(account in keys for account in accounts) or tx.slot<=row['slot']:
                continue
            balances=[post.get(account) for account in accounts if account in keys]
            if not any(balance and balance[:2]==(wallet,mint) and balance[2]>0 for balance in balances):
                with self.store.db('trading') as c:
                    c.execute("UPDATE votes SET amount='0',slot=?,signature=? WHERE mint=? AND wallet=? AND slot<?",
                              (tx.slot,tx.signature,mint,wallet,tx.slot))
                row.update(amount='0',slot=tx.slot,signature=tx.signature)
