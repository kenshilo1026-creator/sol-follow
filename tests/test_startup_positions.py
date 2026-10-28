"""Startup balance checks are offline and never broadcast or notify Telegram."""
import base64
import json
from dataclasses import replace
from copy import deepcopy
import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from chain_common.primitives import TOKEN,TOKEN_2022
from features.strategy.startup_positions import prune_empty_positions
from features.strategy.dev_sell import DevSell
from tests.helpers import Notices,address
from tests.test_pump_buy import setup_order


async def position(config,store):
    key=Keypair()
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    built=await builder()
    tx=VersionedTransaction.from_bytes(base64.b64decode(built['transaction']))
    signed=VersionedTransaction(tx.message,[key])
    store.update_order(oid,state='submitted',raw=base64.b64encode(bytes(signed)).decode(),signature=str(signed.signatures[0]))
    assert store.fill(oid,5000,1000)
    with store.db('trading') as c:
        c.execute('INSERT OR REPLACE INTO order_routes VALUES (?,?)',(oid,json.dumps(route.request())))
        c.execute('INSERT INTO dev_sell_watches VALUES (?,?,?)',(oid,address(),99))
        c.execute("INSERT INTO dev_sell_events(signature,buy_id,slot,state,due,first_seen) VALUES ('sale',?,101,'pending',0,0)",(oid,))
    return cfg,route,oid


def token(wallet,mint,amount='0',program=TOKEN):
    return {'pubkey':address(),'account':{'owner':program,'executable':False,'data':{'parsed':{
        'type':'account','info':{'mint':mint,'owner':wallet,'tokenAmount':{'amount':amount}}}}}}


class Rpc:
    def __init__(self,cfg,mint):
        self.calls=[];self.cfg=cfg;self.mint=mint
        self.status={'value':[{'slot':100,'confirmationStatus':'finalized','err':None}]}
        self.balance={'context':{'slot':110},'value':[]}
        self.error=None;self.mutate=None
    async def call(self,method,params):
        self.calls.append(method)
        if self.error:raise self.error
        if method=='getSignatureStatuses':return self.status
        assert method=='getTokenAccountsByOwner'
        assert params==[self.cfg.wallet_address,{'mint':self.mint},
                       {'encoding':'jsonParsed','commitment':'finalized','minContextSlot':100}]
        if self.mutate:self.mutate()
        return self.balance


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['closed-accounts','spl-zero','token2022-zero'])
async def test_zero_balance_closes_monitoring_once_without_losing_history(config,store,kind):
    cfg,route,oid=await position(config,store);notices=Notices();rpc=Rpc(cfg,route.trade.mint)
    if kind!='closed-accounts':rpc.balance['value']=[token(cfg.wallet_address,route.trade.mint,program=TOKEN if kind=='spl-zero' else TOKEN_2022)]
    with store.db('trading') as c:c.execute("INSERT INTO dev_exits(buy_id,state,updated) VALUES (?,'waiting',0)",(oid,))
    assert await prune_empty_positions(cfg,store,rpc,notices)==1
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']=='0'
    assert store.order(oid)['state']=='finalized' and store.order(oid)['signature']
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='cancelled'
    assert not store.rows('trading','SELECT * FROM dev_sell_watches')
    assert store.rows('trading','SELECT state FROM dev_sell_events')[0]['state']=='ignored'
    assert DevSell(cfg,store,None,notices,None).watch_addresses()==set()
    assert not any(kwargs.get('alert') for args,kwargs in notices.items)
    assert await prune_empty_positions(cfg,store,rpc,notices)==0
    assert len(rpc.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['nonzero-other-account','error','null','missing-value','old-slot','malformed',
    'wrong-owner','wrong-mint','wrong-program','duplicate-account','unknown-status','confirmed-only',
    'pending-buy','pending-sell','pending-exit','changed-wallet','dry','changed-position'])
async def test_unknown_or_nonzero_balances_and_pending_orders_are_retained(config,store,case):
    cfg,route,oid=await position(config,store);notices=Notices();rpc=Rpc(cfg,route.trade.mint)
    row=token(cfg.wallet_address,route.trade.mint)
    if case=='nonzero-other-account':rpc.balance['value']=[row,token(cfg.wallet_address,route.trade.mint,'1')]
    if case=='error':rpc.error=RuntimeError('endpoint-secret-must-not-be-logged')
    if case=='null':rpc.balance=None
    if case=='missing-value':rpc.balance={}
    if case=='old-slot':rpc.balance['context']['slot']=99
    if case in ('malformed','wrong-owner','wrong-mint','wrong-program','duplicate-account'):
        rpc.balance['value']=[row]
        info=row['account']['data']['parsed']['info']
        if case=='malformed':info['tokenAmount']['amount']='0.0'
        if case=='wrong-owner':info['owner']=address()
        if case=='wrong-mint':info['mint']=address()
        if case=='wrong-program':row['account']['owner']=address()
        if case=='duplicate-account':rpc.balance['value'].append(deepcopy(row))
    if case=='unknown-status':rpc.status['value']=[None]
    if case=='confirmed-only':rpc.status['value'][0]['confirmationStatus']='confirmed'
    if case in ('pending-buy','pending-sell'):
        with store.db('trading') as c:c.execute("INSERT INTO orders(id,mint,mode,side,state) VALUES ('pending',?,? ,?,'unknown')",
            (route.trade.mint,cfg.mode,'buy' if case=='pending-buy' else 'sell'))
    if case=='pending-exit':
        with store.db('trading') as c:c.execute("INSERT INTO dev_exits(buy_id,state,signature,updated) VALUES (?,'unknown','pending',0)",(oid,))
    if case=='changed-wallet':cfg=replace(cfg,wallet_address=address())
    if case=='dry':cfg=replace(cfg,dry_run=True)
    if case=='changed-position':
        def change():
            with store.db('trading') as c:c.execute("UPDATE positions SET amount='6000',updated=updated+1")
        rpc.mutate=change
    assert await prune_empty_positions(cfg,store,rpc,notices)==0
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']!='0'
    assert store.rows('trading','SELECT * FROM dev_sell_watches')
    assert all('endpoint-secret' not in str(args) for args,kwargs in notices.items)
