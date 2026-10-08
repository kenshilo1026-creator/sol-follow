"""Resumable signer-history qualification at the CEX funding transaction."""
import json
import time

DAYS = 30
PAGE = 100
BUDGET = 12


class HistoryPending(RuntimeError):
    pass


def signed_by(raw, signature, slot, wallet):
    if not isinstance(raw,dict) or raw.get('slot')!=slot:
        raise HistoryPending('history-transaction-unavailable')
    tx=raw.get('transaction',{})
    if not tx.get('signatures') or tx['signatures'][0]!=signature:
        raise HistoryPending('history-signature-mismatch')
    keys=tx.get('message',{}).get('accountKeys')
    if not isinstance(keys,list) or not keys or any(not isinstance(k,dict) or type(k.get('signer')) is not bool for k in keys):
        raise HistoryPending('history-signers-unavailable')
    if wallet not in {k.get('pubkey') for k in keys}:
        raise HistoryPending('history-wallet-mismatch')
    # Failed transactions still involved the wallet's signature.
    return any(k.get('pubkey')==wallet and k['signer'] for k in keys)


async def qualify_activity(item,rpc,store):
    cutoff=item.time-DAYS*86400
    rows=store.rows('funding','SELECT detail FROM funding_activity_checks WHERE event=?',(item.event,))
    state=json.loads(rows[0]['detail']) if rows else None
    if not state or state.get('cutoff')!=cutoff or state.get('funding_slot')!=item.slot:
        # SPL transfers may include only the recipient token account, not its
        # owner. The decoder already proves the owner using confirmed meta;
        # that deposit cannot appear in the owner's address history. Bound the
        # existing signer-history scan by its confirmed slot in this case.
        external_anchor = item.asset == 'USDC' and not item.wallet_in_keys
        state=dict(cutoff=cutoff,funding_slot=item.slot,cursor=None,last_slot=None,anchored=external_anchor,pending=[],exhausted=False,
                   complete=False,allowed=False,reason='',saw_prior=False)
    def save():
        with store.db('funding') as db:
            db.execute('INSERT OR REPLACE INTO funding_activity_checks VALUES (?,?,?,?)',
                       (item.event,time.time(),item.time+store_activity_retention(),json.dumps(state)))
    def restart():
        with store.db('funding') as db:
            db.execute('DELETE FROM funding_activity_checks WHERE event=?',(item.event,))
        raise HistoryPending('funding-not-in-address-history')
    def finish(allowed,reason):
        state.update(complete=True,allowed=allowed,reason=reason,pending=[]);save()
        return allowed,reason
    if state['complete']:
        return state['allowed'],state['reason']
    work=0
    while work<BUDGET:
        if not state['pending']:
            if state['exhausted']:
                if not state['anchored']:
                    restart()
                return finish(True,'no-prior-signatures' if not state['saw_prior'] else 'no-signed-activity-30d')
            options={'limit':PAGE,'commitment':'confirmed','minContextSlot':item.slot}
            if state['cursor']:options['before']=state['cursor']
            page=await rpc.call('getSignaturesForAddress',[item.wallet,options]);work+=1
            if not isinstance(page,list) or len(page)>PAGE:
                raise HistoryPending('invalid-wallet-history')
            seen=set();last=state['last_slot']
            for row in page:
                if (not isinstance(row,dict) or not isinstance(row.get('signature'),str) or not row['signature']
                        or row['signature']==state['cursor'] or row['signature'] in seen
                        or type(row.get('slot')) is not int or row['slot']<0
                        or (last is not None and row['slot']>last)):
                    raise HistoryPending('invalid-wallet-history-page')
                seen.add(row['signature']);last=row['slot']
            state.update(pending=page,exhausted=len(page)<PAGE,last_slot=last)
            if page:state['cursor']=page[-1]['signature']
            save()
            continue
        row=state['pending'][0]
        sig,slot=row['signature'],row['slot']
        if sig==item.signature:
            if slot!=item.slot:raise HistoryPending('funding-slot-mismatch')
            state['anchored']=True
        elif slot>item.slot:
            pass  # Activity after this CEX deposit is outside the admission test.
        else:
            if not state['anchored'] and slot<item.slot:
                restart()
            stamp=row.get('blockTime')
            if stamp is None:
                stamp=await rpc.call('getBlockTime',[slot]);work+=1
                row['blockTime']=stamp
            if type(stamp) is not int or stamp<=0 or stamp>item.time:
                raise HistoryPending('history-time-unavailable')
            state['saw_prior']=True
            if stamp<cutoff:
                if not state['anchored']:restart()
                return finish(True,'no-signed-activity-30d')
            raw=await rpc.transaction(sig);work+=1
            if signed_by(raw,sig,slot,item.wallet):
                if slot==item.slot:
                    # RPC history does not establish execution order within a slot.
                    raise HistoryPending('same-slot-signed-activity-order-unknown')
                state['signed_activity']={'signature':sig,'slot':slot,'time':stamp}
                return finish(False,'signed-activity-within-30d')
        state['pending'].pop(0);save()
    raise HistoryPending('wallet-history-check-incomplete')


def store_activity_retention():
    return 31*86400
