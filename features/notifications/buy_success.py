"""One completed-buy summary from existing local metadata and the verified receipt."""
import json
import sqlite3


def units(amount,decimals):
    value=str(int(amount)).rjust(decimals+1,'0')
    if not decimals:return value
    return (value[:-decimals]+'.'+value[-decimals:]).rstrip('0').rstrip('.')


def context(store,row,tx,wallet,tokens):
    detail={'order':row['id'],'signature':row['signature'],'mint':row['mint'],
            'mode':row['mode'],'wallet':wallet,'tokens':tokens,
            'buy_sol':units(row['amount'],9),'slot':tx.slot,
            'transaction_url':'https://solscan.io/tx/'+row['signature']}
    decimals={b.get('uiTokenAmount',{}).get('decimals')
              for phase in ('pre','post') for b in tx.meta.get(phase+'TokenBalances') or []
              if b.get('mint')==row['mint'] and b.get('owner')==wallet}
    if len(decimals)==1:
        places=next(iter(decimals))
        if type(places) is int and 0<=places<=255:
            detail['token_amount']=units(tokens,places)
    fee=tx.meta.get('fee')
    if type(fee) is int and fee>=0:detail['network_fee_sol']=units(fee,9)
    # Optional display metadata must not prevent recording a verified fill.
    try:
        creates=store.rows('funding','SELECT detail FROM launch_creates WHERE mint=? ORDER BY slot DESC LIMIT 1',(row['mint'],))
        if creates:
            create=json.loads(creates[0]['detail'])
            if not isinstance(create,dict):create={}
            for key in ('name','symbol','launchpad'):
                if isinstance(create.get(key),str):detail[key]=create[key][:128]
            detail['dev']=create.get('user') or create.get('creator')
        sources=store.rows('trading','SELECT slot FROM order_sources WHERE order_id=?',(row['id'],))
        slots={r['slot'] for r in sources}
        if len(slots)==1:
            detail['source_slot']=next(iter(slots))
            detail['slot_delta']=tx.slot-detail['source_slot']
    except (ValueError,KeyError,TypeError,sqlite3.Error):
        pass
    return detail
