"""CEX USDC withdrawal proof, amount boundaries and owner-history admission."""
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from chain_common.primitives import SYSTEM, TOKEN, TOKEN_2022, USDC
from chain_common.transaction import Tx
from features.funding.decoder import decode
from features.funding.activity_filter import HistoryPending
from features.runtime.service import Service
from share_common.config import load
from tests.helpers import address, transaction, Notices


def withdrawal(config, amount=50_000_000, *, checked=True, inner=False, owner_in_keys=False, new_account=False):
    cex, wallet, source, destination = [address() for _ in range(4)]
    keys = [cex, source, destination, USDC, TOKEN]
    if owner_in_keys:
        keys.append(wallet)
    info = dict(source=source, destination=destination, authority=cex)
    if checked:
        info.update(mint=USDC, tokenAmount={'amount':str(amount), 'decimals':6})
    else:
        info['amount'] = str(amount)
    ix = {'programId':TOKEN, 'parsed':{'type':'transferChecked' if checked else 'transfer', 'info':info}}
    def balance(index, owner, value):
        return dict(accountIndex=index, owner=owner, mint=USDC, programId=TOKEN,
                    uiTokenAmount={'amount':str(value), 'decimals':6})
    raw = transaction(keys, [cex], [ix] if not inner else [{'programId':address(), 'accounts':[], 'data':''}],
        inner=[{'index':0, 'instructions':[ix]}] if inner else [],
        pre_tokens=[balance(1, cex, 2_000_000_000)]+([] if new_account else [balance(2, wallet, 10_000_000)]),
        post_tokens=[balance(1, cex, 2_000_000_000-amount), balance(2, wallet, amount+(0 if new_account else 10_000_000))],
        when=int(time.time()), sig='usdc-withdrawal')
    return raw, replace(config, cex={cex:'test'}, min_funding_usdc=50_000_000, max_funding_usdc=800_000_000), wallet


@pytest.mark.parametrize('amount,accepted', [(49_999_999,False),(50_000_000,True),(800_000_000,True),(800_000_001,False)])
@pytest.mark.parametrize('checked,inner,new_account', [(True,False,False),(False,False,False),(True,True,True)])
def test_amount_bounds_and_transfer_forms(config, amount, accepted, checked, inner, new_account):
    raw, cfg, wallet = withdrawal(config, amount, checked=checked, inner=inner, new_account=new_account)
    items = decode(Tx(raw), cfg)
    assert bool(items) is accepted
    if accepted:
        item, = items
        assert item.wallet == wallet and item.asset == 'USDC' and not item.wallet_in_keys
        assert item.amount == amount and item.provider == 'cex:test'
        assert item.event.endswith(':USDC')


@pytest.mark.parametrize('change', ['mint','program','decimals','authority','unsigned','source-owner','cex-destination',
    'missing-owner','missing-source','missing-destination','received-short','source-not-debited','owner-changed',
    'mint-changed','checked-mint','checked-decimals'])
def test_no_admission_without_cex_and_balance_proof(config, change):
    raw, cfg, wallet = withdrawal(config)
    pre, post = raw['meta']['preTokenBalances'], raw['meta']['postTokenBalances']
    ix = raw['transaction']['message']['instructions'][0]
    if change == 'mint':
        for row in pre+post: row['mint'] = address()
    elif change == 'program': ix['programId'] = TOKEN_2022
    elif change == 'decimals': post[1]['uiTokenAmount']['decimals'] = 9
    elif change == 'authority': ix['parsed']['info']['authority'] = address()
    elif change == 'unsigned': raw['transaction']['message']['accountKeys'][0]['signer'] = False
    elif change == 'source-owner': pre[0]['owner'] = address()
    elif change == 'cex-destination': cfg = replace(cfg, cex={**cfg.cex, wallet:'other-exchange'})
    elif change == 'missing-owner': post[1].pop('owner')
    elif change == 'missing-source': pre.pop(0)
    elif change == 'missing-destination': post.pop(1)
    elif change == 'received-short': post[1]['uiTokenAmount']['amount'] = '59999999'
    elif change == 'source-not-debited': post[0]['uiTokenAmount']['amount'] = pre[0]['uiTokenAmount']['amount']
    elif change == 'owner-changed': pre[1]['owner'] = address()
    elif change == 'mint-changed': pre[1]['mint'] = address()
    elif change == 'checked-mint': ix['parsed']['info']['mint'] = address()
    elif change == 'checked-decimals': ix['parsed']['info']['tokenAmount']['decimals'] = 9
    assert decode(Tx(raw), cfg) == []


def test_batch_recipients_and_native_sol_are_independent(config):
    raw, cfg, wallet = withdrawal(config)
    other, other_account = address(), address()
    raw['transaction']['message']['accountKeys'].extend([
        {'pubkey':other_account,'signer':False}, {'pubkey':other,'signer':False}])
    raw['meta']['preBalances'].extend([0,0]); raw['meta']['postBalances'].extend([0,1_000_000_000])
    second = deepcopy(raw['transaction']['message']['instructions'][0])
    second['parsed']['info']['destination'] = other_account
    second['parsed']['info']['tokenAmount']['amount'] = '800000000'
    raw['transaction']['message']['instructions'].extend([second,
        {'programId':SYSTEM,'parsed':{'type':'transfer','info':{
            'source':next(iter(cfg.cex)), 'destination':other,'lamports':1_000_000_000}}}])
    raw['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '1150000000'
    raw['meta']['postTokenBalances'].append(dict(accountIndex=5, owner=other, mint=USDC, programId=TOKEN,
                                                uiTokenAmount={'amount':'800000000','decimals':6}))
    items = decode(Tx(raw), cfg)
    assert {(i.wallet,i.asset) for i in items} == {(wallet,'USDC'),(other,'USDC'),(other,'SOL')}
    assert len({i.event for i in items}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('history', ['empty','unsigned','signed','same-slot','later','owner-key-missing-deposit','owner-key-deposit'])
async def test_service_checks_wallet_owner_history_and_deduplicates(config, store, history):
    owner_in_keys = history.startswith('owner-key')
    raw, cfg, wallet = withdrawal(config, owner_in_keys=owner_in_keys)
    stamp, slot = raw['blockTime'], raw['slot']
    class RPC:
        calls = []
        async def call(self, method, params):
            self.calls.append((method,params))
            assert params[0] == wallet  # Never qualify the recipient token account.
            if method == 'getAccountInfo':
                return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
            assert method == 'getSignaturesForAddress'
            assert params[1]['minContextSlot'] == slot
            if history == 'owner-key-deposit':
                return [{'signature':raw['transaction']['signatures'][0], 'slot':slot, 'blockTime':stamp}]
            if history in ('empty','owner-key-missing-deposit'): return []
            return [{'signature':'activity','slot':slot+(1 if history=='later' else 0 if history=='same-slot' else -1),
                     'blockTime':stamp+(1 if history=='later' else -1)}]
        async def transaction(self, sig):
            if sig == 'usdc-withdrawal': return raw
            assert sig == 'activity'
            return transaction([wallet],[wallet] if history in ('signed','same-slot') else [],[],
                               slot=slot+(1 if history=='later' else 0 if history=='same-slot' else -1),
                               when=stamp+(1 if history=='later' else -1),sig=sig)
    service = Service(cfg, store); service.notices = Notices(); rpc = RPC()
    store.enqueue(['usdc-withdrawal'])
    if history == 'owner-key-missing-deposit':
        with pytest.raises(HistoryPending): await service.process({'signature':'usdc-withdrawal'},rpc)
    else:
        await service.process({'signature':'usdc-withdrawal'},rpc)
        await service.process({'signature':'usdc-withdrawal'},rpc)
    allowed = history != 'owner-key-missing-deposit'
    assert (wallet in store.hotlist(time.time())) is allowed
    assert len(store.rows('funding','SELECT * FROM funding')) == int(allowed)
    assert len(service.notices.items) == int(allowed)
    assert store.rows('trading','SELECT * FROM orders') == []


@pytest.mark.asyncio
async def test_rejected_amount_audited_without_qualification_rpc(config, store):
    raw, cfg, wallet = withdrawal(config, 800_000_001)
    class RPC:
        async def transaction(self, sig): return raw
        async def call(self, *args): raise AssertionError('no qualification RPC for out-of-range USDC')
    store.enqueue(['usdc-withdrawal'])
    await Service(cfg,store).process({'signature':'usdc-withdrawal'},RPC())
    assert store.hotlist(time.time()) == {}
    rows = store.rows('audit',"SELECT reason FROM decisions WHERE wallet=? AND stage='funding'",(wallet,))
    assert rows == [{'reason':'funding-above-maximum'}]


def test_usdc_configuration_defaults_and_decimal_units(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    cfg = load(root=tmp_path,env={})
    assert (cfg.min_funding_usdc,cfg.max_funding_usdc) == (50_000_000,800_000_000)
    cfg = load(root=tmp_path,env={'SOL_HOTLIST_MIN_FUNDING_USDC':'50.000001','SOL_HOTLIST_MAX_FUNDING_USDC':'50.000001'})
    assert cfg.min_funding_usdc == cfg.max_funding_usdc == 50_000_001
    for values in ({'SOL_HOTLIST_MIN_FUNDING_USDC':'801'},
                   *({'SOL_HOTLIST_MAX_FUNDING_USDC':v} for v in ('0','-1','NaN','Infinity','x','50.0000001','18446744073710'))):
        with pytest.raises(ValueError): load(root=tmp_path,env=values)
