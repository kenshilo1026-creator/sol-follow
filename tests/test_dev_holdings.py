import asyncio
import base64
import json
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import pytest
from chain_common.primitives import TOKEN, TOKEN_2022, WSOL, pubkey, discriminator
from launchpads import pump_fun, stonk
from features.database.storage import Store
from features.strategy.dev_holdings import DevHoldingsGate, permitted
from features.strategy.signals import Signals
from share_common.config import load, optional_dev_limit
from share_common.notify import Notices
from tests.helpers import address
from tests.test_signals import trade, fund




class Rpc:
    def __init__(self,route,creator,amounts):
        self.calls=[]
        mint=bytearray(82);mint[44]=6;mint[45]=1
        if 'stonk' in route.kind:
            pool=bytearray(429);pool[:8]=discriminator('account','PoolState');pool[18]=6
            for offset,value in [(173,stonk.PLATFORM),(205,route.trade.mint),(237,route.quote_mint),(333,creator)]:
                pool[offset:offset+32]=bytes(pubkey(value))
            program=stonk.PROGRAM
        else:
            pool=bytearray(150);pool[:8]=discriminator('account','BondingCurve')
            pool[49:81]=bytes(pubkey(creator));pool[83:115]=bytes(pubkey(route.quote_mint))
            program=pump_fun.PROGRAM
        def row(raw,owner):return {'owner':owner,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']}
        self.identity={'context':{'slot':route.trade.slot},'value':[row(mint,route.token_program),row(pool,program)]}
        self.result={'context':{'slot':route.trade.slot},'value':[
            {'pubkey':address(),'account':{'owner':route.token_program,'executable':False,
             'data':{'parsed':{'type':'account','info':{'owner':creator,'mint':route.trade.mint,
                     'state':'initialized','tokenAmount':{'amount':str(amount),'decimals':6}}}}}}
            for amount in amounts]}

    async def call(self,method,params):
        self.calls.append((method,params))
        await asyncio.sleep(0)
        return deepcopy(self.identity if method=='getMultipleAccounts' else self.result)


def setup(config,store,amounts=(60_000_000_000_000,),kind='pump_native_curve',program=TOKEN):
    cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    route=SimpleNamespace(trade=trade(address(),address(),'first',time.time()),
                          kind=kind,quote_mint=WSOL,token_program=program)
    pool=stonk.pool_address(route.trade.mint,route.quote_mint) if 'stonk' in kind else pump_fun.curve_address(route.trade.mint)
    route.trade=replace(route.trade,pool=pool)
    creator=address()
    notices=Notices(cfg,store)
    return cfg,route,DevHoldingsGate(cfg,store,notices),Rpc(route,creator,amounts)


def test_config(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    assert load(tmp_path,env={}).max_dev_holding_tokens==Decimal('60000000')
    assert load(tmp_path,env={'SOL_DEV_MAX_HOLDING_TOKENS':''}).max_dev_holding_tokens is None
    assert optional_dev_limit('0')==0
    assert optional_dev_limit('60000000.000001')==Decimal('60000000.000001')
    for value in ['-1','NaN','Infinity','abc','1e31']:
        with pytest.raises(ValueError):optional_dev_limit(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('amounts,allowed',[([],True),([60_000_000_000_000],True),
    ([30_000_000_000_000,30_000_000_000_001],False)])
@pytest.mark.parametrize('kind,program',[('pump_native_curve',TOKEN),('stonk_native_curve',TOKEN_2022)])
async def test_boundary_aggregation_once_and_restart(config,store,amounts,allowed,kind,program):
    cfg,route,gate,rpc=setup(config,store,amounts,kind,program)
    assert await gate.check(route,rpc) is allowed
    later=SimpleNamespace(**{**vars(route),'trade':replace(route.trade,signature='later',wallet=address(),slot=3)})
    assert await gate.check(later,rpc) is allowed
    restarted=Store(cfg.data)
    assert await DevHoldingsGate(cfg,restarted,gate.notices).check(later,rpc) is allowed
    assert permitted(restarted,route.trade.mint,cfg.max_dev_holding_tokens) is allowed
    assert len(rpc.calls)==2
    method,params=rpc.calls[1]
    assert method=='getTokenAccountsByOwner' and params[1]=={'mint':route.trade.mint}
    assert params[2]=={'encoding':'jsonParsed','commitment':cfg.hotlist_commitment,'minContextSlot':route.trade.slot}
    assert gate.row(route.trade.mint)['signature']=='first'


@pytest.mark.asyncio
async def test_parallel_checks_and_limit_changes(config,store):
    cfg,route,gate,rpc=setup(config,store)
    assert await asyncio.gather(gate.check(route,rpc),gate.check(route,rpc))==[True,True]
    cfg=replace(cfg,max_dev_holding_tokens=Decimal('59999999'))
    assert not permitted(store,route.trade.mint,cfg.max_dev_holding_tokens)
    assert not await DevHoldingsGate(cfg,store,gate.notices).check(route,rpc)
    for limit in [None,Decimal('90000000')]:
        cfg=replace(cfg,max_dev_holding_tokens=limit)
        assert not await DevHoldingsGate(cfg,store,gate.notices).check(route,rpc)
        assert not permitted(store,route.trade.mint,limit)
    assert len(rpc.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['stale','duplicate','owner','mint','program','decimals','amount','shape','creator'])
async def test_invalid_or_missing_evidence_never_retried(config,store,fault):
    cfg,route,gate,rpc=setup(config,store)
    row=rpc.result['value'][0];info=row['account']['data']['parsed']['info']
    if fault=='stale':rpc.result['context']['slot']-=1
    elif fault=='duplicate':rpc.result['value'].append(deepcopy(row))
    elif fault in ('owner','mint'):info[fault]=address()
    elif fault=='program':row['account']['owner']=TOKEN_2022
    elif fault=='decimals':info['tokenAmount']['decimals']=9
    elif fault=='amount':info['tokenAmount']['amount']='-1'
    elif fault=='shape':rpc.result['value']=None
    else:
        rpc.identity['value'][1]=None
    assert not await gate.check(route,rpc)
    assert gate.row(route.trade.mint)['state']=='unavailable'
    assert not await DevHoldingsGate(cfg,Store(cfg.data),gate.notices).check(route,rpc)
    assert len(rpc.calls)==(1 if fault=='creator' else 2)
    assert gate.notices.queue.qsize()==1


@pytest.mark.asyncio
async def test_rpc_failure_and_cancellation_persist(config,store):
    cfg,route,gate,rpc=setup(config,store)
    async def fail(*args):raise RuntimeError('429 https://secret-provider-key')
    rpc.call=fail
    assert not await gate.check(route,rpc)
    assert 'secret' not in gate.row(route.trade.mint)['detail']
    cfg,route,gate,rpc=setup(config,store)
    started=asyncio.Event()
    async def hang(*args):
        started.set()
        await asyncio.Event().wait()
    rpc.call=hang
    task=asyncio.create_task(gate.check(route,rpc))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert gate.row(route.trade.mint)['state']=='checking'
    rpc.call=fail
    restarted=DevHoldingsGate(cfg,Store(cfg.data),gate.notices);restarted.recover()
    assert not await restarted.check(route,rpc)


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed',[True,False])
async def test_vote_reservation_and_execution_guards(config,store,allowed):
    from trade_execution.executor import Executor
    cfg,route,gate,rpc=setup(config,store,[60_000_000_000_000+(not allowed)])
    now=time.time();engine=Signals(store,cfg,started=now-2)
    fund(store,route.trade.wallet,now)
    assert engine.observe(route.trade) is None
    assert not store.reserve(cfg,route.trade.mint,route.trade.pool,cfg.buy_amount)
    assert not store.rows('trading','SELECT * FROM votes')
    assert await gate.check(route,rpc) is allowed
    for i in range(cfg.n):
        wallet=address();fund(store,wallet,now)
        order=engine.observe(replace(route.trade,event=str(i),signature=str(i),wallet=wallet,slot=i+2))
    assert bool(order) is allowed
    executor=Executor(cfg,store,rpc,gate.notices,None)
    assert executor.fresh(route) is allowed
    assert bool(store.rows('trading','SELECT * FROM votes')) is allowed
    assert route.trade.wallet in store.hotlist(now)


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed',[True,False])
async def test_processed_service_first_buy_gate(config,store,allowed):
    from chain_common.transaction import Tx
    from features.runtime.service import Service
    from tests.test_native_buy import sample
    from tests.test_processed import admit
    from trade_execution.route import decode_all
    now=int(time.time());raw=sample();raw['blockTime']=None
    raw['_stream']={'commitment':'processed','live':True}
    routes=decode_all(Tx(raw,observed_at=now))
    for route in routes:admit(store,route,now)
    route=routes[0];creator=address()
    rpc=Rpc(route,creator,[60_000_000_000_000+(not allowed)])
    cfg=replace(config,n=3,hotlist_commitment='processed',max_dev_holding_tokens=Decimal('60000000'))
    service=Service(cfg,store);service.signals.started=now-1
    executed=[]
    class Engine:
        async def buy(self,oid,route):executed.append(oid)
    service.executor=Engine()
    store.stream_transaction(raw)
    store.mark_live(raw['transaction']['signatures'][0])
    await service.process({'signature':raw['transaction']['signatures'][0]},rpc)
    assert len(executed)==1  # Three wallets buy before the background result.
    await service.dev_holdings.check(route,rpc)
    assert len(rpc.calls)==2
    assert permitted(store,route.trade.mint,cfg.max_dev_holding_tokens) is allowed
    if not allowed:assert not store.rows('trading','SELECT * FROM votes')
    await service.dev_holdings.close()


@pytest.mark.asyncio
async def test_third_wallet_can_buy_while_holdings_rpc_is_in_flight(config,store):
    from trade_execution.executor import Executor
    cfg,route,gate,rpc=setup(config,store,[60_000_000_000_001])
    cfg=replace(cfg,n=3)
    gate.config=cfg
    arrived=asyncio.Event();release=asyncio.Event();original=rpc.call
    async def slow(method,params):
        if method=='getTokenAccountsByOwner':
            arrived.set()
            await release.wait()
        return await original(method,params)
    rpc.call=slow
    now=time.time();engine=Signals(store,cfg,started=now-2)
    assert gate.start(route,rpc)
    await arrived.wait()
    executor=Executor(cfg,store,rpc,gate.notices,None)
    assert executor.fresh(route)
    for i in range(3):
        wallet=address();fund(store,wallet,now)
        order=engine.observe(replace(route.trade,event=str(i),signature=str(i),wallet=wallet,slot=i+2))
        assert bool(order)==(i==2)
    assert gate.row(route.trade.mint)['state']=='checking'
    release.set();await gate.check(route,rpc)
    assert not executor.fresh(route)
    assert gate.row(route.trade.mint)['state']=='blocked'
    assert len(rpc.calls)==2 and not store.rows('trading','SELECT * FROM votes')
