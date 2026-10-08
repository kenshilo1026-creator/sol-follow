"""Indexed local decisions, independent of Telegram and best effort on disk errors."""
import json
import logging
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, time REAL NOT NULL,
 wallet TEXT NOT NULL, mint TEXT NOT NULL, signature TEXT NOT NULL, event TEXT NOT NULL,
 stage TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
 UNIQUE(event,wallet,mint,stage,outcome,reason));
CREATE INDEX IF NOT EXISTS decisions_wallet_time ON decisions(wallet,time);
CREATE INDEX IF NOT EXISTS decisions_signature ON decisions(signature);
CREATE INDEX IF NOT EXISTS decisions_time ON decisions(time);
"""


def record(store,stage,outcome,reason,*,wallet='',mint='',signature='',event='',detail=None):
    try:
        encoded=json.dumps(detail or {},ensure_ascii=False,separators=(',',':'))
        if len(encoded)>8000:encoded=json.dumps({'detail_truncated':True})
        with store.db('audit') as c:
            c.execute('PRAGMA busy_timeout=5')
            # Same replay/retry decision is a single durable fact, not a log flood.
            c.execute('INSERT OR IGNORE INTO decisions(time,wallet,mint,signature,event,stage,outcome,reason,detail) '
                      'VALUES (?,?,?,?,?,?,?,?,?)',
                      (time.time(),wallet,mint,signature,event or signature,stage,outcome,reason,encoded))
    except Exception as exc:
        logging.getLogger(__name__).error('[SOL] decision audit unavailable type=%s',type(exc).__name__)


def trade_record(store,trade,stage,outcome,reason,detail=None):
    record(store,stage,outcome,reason,wallet=trade.wallet,mint=trade.mint,
           signature=trade.signature,event=trade.event,detail={'slot':trade.slot,'chain_time':trade.time,
           'side':trade.side,'quote_paid_raw':str(trade.quote),**(detail or {})})


def funding_candidates(store,tx,config,accepted):
    """Record configured-CEX transfers rejected before qualification, without RPC."""
    from chain_common.primitives import SYSTEM
    known={item.event for item in accepted}
    from features.funding.decoder import usdc_candidates
    for item, reason in usdc_candidates(tx, config):
        if reason and item.event not in known:
            record(store,'funding','blocked',reason,wallet=item.wallet,signature=item.signature,event=item.event,
                   detail={**item.dict(),'minimum_raw':str(config.min_funding_usdc),
                           'maximum_raw':str(config.max_funding_usdc),'decimals':6})
    for path,ix in tx.instructions():
        if ix.get('programId')!=SYSTEM or ix.get('parsed',{}).get('type')!='transfer':continue
        info=ix['parsed']['info'];source=info['source'];wallet=info['destination']
        if source not in config.cex:continue
        amount=int(info['lamports']);event=f'{tx.signature}:{path}:{wallet}:SOL'
        if event in known:continue
        if source not in tx.signers:reason='cex-source-not-signer'
        elif wallet in config.cex or source==wallet:reason='destination-is-cex'
        elif amount<config.min_funding:reason='funding-below-minimum'
        elif amount>config.max_funding:reason='funding-above-maximum'
        else:reason='funding-balance-proof-unavailable'
        record(store,'funding','blocked',reason,wallet=wallet,signature=tx.signature,event=event,
               detail={'source':source,'provider':config.cex[source],'amount_lamports':str(amount),
                       'minimum_lamports':str(config.min_funding),'maximum_lamports':str(config.max_funding)})


def skipped_transaction(store,raw,config,reason):
    """Attribute only explicit signers, current watched accounts and CEX recipients."""
    try:
        msg=raw['transaction']['message'];sig=raw['transaction']['signatures'][0]
        watched=set(store.hotlist(time.time()))
        wallets={r['pubkey'] for r in msg['accountKeys'] if isinstance(r,dict)
                 and (r.get('signer') or r.get('pubkey') in watched)}
        instructions=list(msg.get('instructions',[]))
        for group in (raw.get('meta') or {}).get('innerInstructions',[]):instructions.extend(group['instructions'])
        for ix in instructions:
            parsed=ix.get('parsed',{});info=parsed.get('info',{})
            if parsed.get('type')=='transfer' and info.get('source') in config.cex:
                if info.get('destination'):wallets.add(info['destination'])
        for wallet in wallets:
            record(store,'transaction','skipped',reason,wallet=wallet,signature=sig,detail={'slot':raw['slot']})
    except Exception as exc:
        logging.getLogger(__name__).error('[SOL] transaction audit unavailable type=%s',type(exc).__name__)
