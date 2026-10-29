"""Externally emptied positions never trigger new dev sells; unknown is not zero."""
import asyncio
from copy import deepcopy
import pytest
from chain_common.primitives import TOKEN,TOKEN_2022
from features.strategy.startup_positions import retire_empty_position
from trade_execution.builder import BuildError
from tests.helpers import Notices,address
from tests.test_startup_positions import position,token
from tests.test_dev_sell import armed


class BalanceRpc:
    def __init__(self,wallet,mint,amount=None):
        self.calls=[]
        self.balance={'context':{'slot':110},'value':[] if amount is None else [token(wallet,mint,amount)]}
        self.status={'value':[{'slot':100,'confirmationStatus':'finalized','err':None}]}
        self.error=None;self.mutate=None
    async def call(self,method,params):
        self.calls.append((method,params))
        if self.error:raise self.error
        if method=='getSignatureStatuses':return self.status
        assert method=='getTokenAccountsByOwner'
        assert params[2]=={'encoding':'jsonParsed','commitment':'finalized','minContextSlot':0}
        if self.mutate:self.mutate()
        return self.balance


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['closed','spl','token2022'])
async def test_manual_zero_before_dev_sale_has_no_exit_or_alert(config,store,kind):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    mint=store.order(oid)['mint'];rpc=BalanceRpc(engine.config.wallet_address,mint)
    if kind!='closed':rpc.balance['value']=[token(engine.config.wallet_address,mint,program=TOKEN if kind=='spl' else TOKEN_2022)]
    reader.call=rpc.call;engine.balance_rpc=rpc
    worker.market_event(dev,'dev-sale',raw['slot'])
    await worker.once();await engine.once()
    assert not builds and not store.rows('trading','SELECT * FROM dev_exits')
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']=='0'
    assert not store.rows('trading','SELECT * FROM dev_sell_watches') and not worker.watches
    assert store.rows('trading','SELECT state,reason FROM dev_sell_events')==[{'state':'ignored','reason':'wallet-balance-zero'}]
    assert store.order(oid)['state']=='finalized'
    assert not any(kwargs.get('alert') for args,kwargs in worker.notices.items+engine.notices.items)
    worker.market_event(dev,'another-dev-sale',raw['slot']+1)
    await worker.once();await engine.once()
    assert len(rpc.calls)==2


@pytest.mark.asyncio
async def test_existing_unsigned_exit_retry_is_cancelled_without_build_or_alert(config,store):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    engine.balance_rpc=BalanceRpc(engine.config.wallet_address,store.order(oid)['mint'])
    with store.db('trading') as c:
        c.execute("INSERT INTO dev_exits(buy_id,state,updated,reason) VALUES (?,'waiting',0,'dev-sell-100-percent')",(oid,))
    await engine.once();await engine.once()
    assert not builds
    row=store.rows('trading','SELECT * FROM dev_exits')[0]
    assert row['state']=='cancelled' and row['reason']=='wallet-balance-zero' and row['signature'] is None
    assert not any(kwargs.get('alert') for args,kwargs in engine.notices.items)


@pytest.mark.asyncio
async def test_manual_sell_lands_during_build_and_suppresses_false_failure(config,store):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    rpc=BalanceRpc(engine.config.wallet_address,store.order(oid)['mint'],'1000');engine.balance_rpc=rpc
    with store.db('trading') as c:c.execute("INSERT INTO dev_exits(buy_id,state,updated) VALUES (?,'waiting',0)",(oid,))
    async def builder(*args):
        rpc.balance['value']=[]
        raise BuildError('sell-balance-unavailable')
    engine.builder=builder
    await engine.once()
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='cancelled'
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']=='0'
    assert not any(kwargs.get('alert') for args,kwargs in engine.notices.items)
    assert [m for m,p in rpc.calls]==['getTokenAccountsByOwner','getTokenAccountsByOwner','getSignatureStatuses']


@pytest.mark.asyncio
async def test_insufficient_or_frozen_is_not_assumed_empty(config,store):
    worker,engine,reader,oid,dev,raw,builds=await armed(config,store)
    engine.balance_rpc=BalanceRpc(engine.config.wallet_address,store.order(oid)['mint'],'1')
    with store.db('trading') as c:c.execute("INSERT INTO dev_exits(buy_id,state,updated) VALUES (?,'waiting',0)",(oid,))
    async def builder(*args):raise BuildError('sell-balance-unavailable')
    engine.builder=builder
    await engine.once()
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='waiting'
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']=='1000'
    assert any(args[0]=='dev exit deferred' and kwargs.get('alert') for args,kwargs in engine.notices.items)


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['positive-other-account','rpc-error','null','old-context','wrong-owner','invalid-amount',
    'duplicate','unknown-buy','unfinalized-buy','wrong-wallet','pending-buy','pending-sell','signed-exit','changed-position','cancelled'])
async def test_live_retirement_keeps_ambiguous_balances_and_pending_orders(config,store,case):
    cfg,route,oid=await position(config,store);rpc=BalanceRpc(cfg.wallet_address,route.trade.mint);notices=Notices()
    if case=='positive-other-account':rpc.balance['value']=[token(cfg.wallet_address,route.trade.mint),token(cfg.wallet_address,route.trade.mint,'1')]
    if case=='rpc-error':rpc.error=RuntimeError('secret-provider-url')
    if case=='null':rpc.balance=None
    if case=='old-context':rpc.balance['context']['slot']=99
    if case in ('wrong-owner','invalid-amount','duplicate'):
        row=token(cfg.wallet_address,route.trade.mint);rpc.balance['value']=[row]
        if case=='wrong-owner':row['account']['data']['parsed']['info']['owner']=address()
        if case=='invalid-amount':row['account']['data']['parsed']['info']['tokenAmount']['amount']='-0'
        if case=='duplicate':rpc.balance['value'].append(deepcopy(row))
    if case=='unknown-buy':rpc.status['value']=[None]
    if case=='unfinalized-buy':rpc.status['value'][0]['confirmationStatus']='confirmed'
    if case=='wrong-wallet':
        from dataclasses import replace
        cfg=replace(cfg,wallet_address=address())
    if case in ('pending-buy','pending-sell'):
        with store.db('trading') as c:
            c.execute("INSERT INTO orders(id,mint,mode,side,state) VALUES ('pending',?,?,?,'unknown')",(route.trade.mint,cfg.mode,case.split('-')[1]))
    if case=='signed-exit':
        with store.db('trading') as c:c.execute("INSERT INTO dev_exits(buy_id,state,updated,signature) VALUES (?,'unknown',0,'saved')",(oid,))
    if case=='changed-position':
        def change():
            with store.db('trading') as c:c.execute("UPDATE positions SET amount='6000',updated=updated+1")
        rpc.mutate=change
    if case=='cancelled':
        rpc.error=asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):await retire_empty_position(cfg,store,rpc,notices,route.trade.mint)
    else:assert not await retire_empty_position(cfg,store,rpc,notices,route.trade.mint)
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']!='0'
    assert store.rows('trading','SELECT * FROM dev_sell_watches')
    assert not any(kwargs.get('alert') for args,kwargs in notices.items)
    assert all('secret-provider-url' not in str(args) for args,kwargs in notices.items)
