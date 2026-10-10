"""Bounded, resumable transaction-count and launch-history admission checks."""
import json
import time
from chain_common.transaction import Tx, Unsupported
from launchpads.create import inspect

HISTORY_POLICY = 4
BUDGET = 12


class HistoryPending(RuntimeError):
    pass


def validate_history_transaction(raw, signature, slot, wallet):
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


def previous_launch(raw, wallet, stamp):
    """Attribute supported successful creates to their user or signing creator."""
    meta=raw.get('meta')
    if not isinstance(meta,dict) or 'err' not in meta:
        raise HistoryPending('history-create-unavailable')
    if meta['err'] is not None:return None
    try:
        # Use the chain timestamp already resolved from the signature/block.
        tx=Tx({**raw,'blockTime':stamp})
        result=inspect(tx)
    except (Unsupported,ValueError,KeyError,IndexError,TypeError,AttributeError) as exc:
        raise HistoryPending('history-create-unavailable') from exc
    for created in result.creates:
        # A caller can supply somebody else's creator address. That field alone
        # must not poison an unsigned wallet's admission history.
        if created.user==wallet or (created.creator==wallet and wallet in tx.signers):
            return {'signature':created.signature,'slot':created.slot,'time':created.time,
                    'mint':created.mint,'launchpad':created.launchpad,
                    'user':created.user,'creator':created.creator,
                    'instruction_path':created.instruction_path}
    if result.rejected:raise HistoryPending('history-create-unavailable')
    return None


async def qualify_activity(item,rpc,store,max_transactions=10):
    """Cap address history, then check every returned transaction for launches.

    The cap includes incoming/failed transactions and the current funding. An
    externally proven USDC deposit absent from accountKeys is counted once.
    Older token-account-only activity is not discoverable by this wallet query.
    """
    if not 1<=max_transactions<=999:
        raise ValueError('invalid-prelaunch-transaction-limit')
    rows=store.rows('funding','SELECT detail FROM funding_activity_checks WHERE event=?',(item.event,))
    state=json.loads(rows[0]['detail']) if rows else None
    if (not state or state.get('policy')!=HISTORY_POLICY or state.get('max_transactions')!=max_transactions
            or state.get('funding_time')!=item.time or state.get('funding_slot')!=item.slot):
        external_anchor=item.asset=='USDC' and not item.wallet_in_keys
        state=dict(policy=HISTORY_POLICY,max_transactions=max_transactions,funding_time=item.time,
                   funding_slot=item.slot,anchored=external_anchor,external_anchor=external_anchor,
                   pending=[],count_checked=False,total_transactions=None,complete=False,
                   allowed=False,reason='')
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
    if not state['count_checked']:
        limit=max_transactions+1
        page=await rpc.call('getSignaturesForAddress',[item.wallet,
            {'limit':limit,'commitment':'confirmed','minContextSlot':item.slot}]);work+=1
        if not isinstance(page,list) or len(page)>limit:
            raise HistoryPending('invalid-wallet-history')
        seen=set();last=None
        for row in page:
            if (not isinstance(row,dict) or not isinstance(row.get('signature'),str) or not row['signature']
                    or row['signature'] in seen or type(row.get('slot')) is not int or row['slot']<0
                    or (last is not None and row['slot']>last)):
                raise HistoryPending('invalid-wallet-history-page')
            if row['signature']==item.signature and row['slot']!=item.slot:
                raise HistoryPending('funding-slot-mismatch')
            seen.add(row['signature']);last=row['slot']
        extra_deposit=int(state['external_anchor'] and item.signature not in seen)
        state.update(count_checked=True,total_transactions=len(seen)+extra_deposit,
                     count_is_lower_bound=len(page)==limit,external_deposit_counted=bool(extra_deposit),pending=page)
        if state['total_transactions']>max_transactions:
            return finish(False,'prelaunch-activity-too-high')
        # A short page proves the available address history fits the cap.
        # Never paginate or download individual transactions for an over-cap wallet.
        save()
    while state['pending'] and work<BUDGET:
        row=state['pending'][0]
        sig,slot=row['signature'],row['slot']
        current=sig==item.signature
        prior=not current and slot<=item.slot
        if current:state['anchored']=True
        if prior and not state['anchored'] and slot<item.slot:restart()
        stamp=row.get('blockTime')
        if stamp is None:
            stamp=await rpc.call('getBlockTime',[slot]);work+=1
            row['blockTime']=stamp
        if type(stamp) is not int or stamp<=0 or (prior and stamp>item.time):
            raise HistoryPending('history-time-unavailable')
        raw=await rpc.transaction(sig);work+=1
        validate_history_transaction(raw,sig,slot,item.wallet)
        # Also inspect the deposit and later rows: a successful launch in any
        # returned transaction disqualifies admission, regardless of its age.
        launch=previous_launch(raw,item.wallet,stamp)
        if launch:
            state['previous_launch']=launch
            return finish(False,'previous-token-launch')
        state['pending'].pop(0);save()
    if state['pending']:raise HistoryPending('wallet-history-check-incomplete')
    if not state['anchored']:restart()
    return finish(True,'history-qualified')


def store_activity_retention():
    return 31*86400
