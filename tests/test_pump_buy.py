import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction
from chain_common.primitives import pubkey, b58encode, discriminator
from chain_common.rpc import Priority
from chain_common.transaction import Tx
from features.funding.decoder import Funding
from features.runtime.service import Service
from trade_execution.route import decode
from trade_execution.executor import Executor, MARKER
from tests.helpers import Notices, address


def sample():
    return json.loads((Path(__file__).parent/'fixtures/pump_non_sol_buy.json').read_text())


def fresh_route():
    raw=sample();raw['blockTime']=int(time.time())
    return decode(Tx(raw))


def test_reference_atomic_buy():
    route=decode(Tx(sample()))
    assert route and route.trade.quote==179332317
    assert route.trade.tokens==5596032892610
    assert route.quote_mint=='SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb'
    assert route.dlmm_pool=='3h9GhSUzozu2eRQ9MCLCeHWYshywm4mqbzPtz2h1zGMF'
    assert len(route.lookup_tables)==2


@pytest.mark.parametrize('change',['program','curve','owner','ata','quote','direction','duplicate','outflow'])
def test_forged_or_ambiguous_route_rejected(change):
    raw=sample();ixs=raw['transaction']['message']['instructions'];pump=ixs[7];swap=ixs[6]
    if change=='program':pump['programId']=address()
    elif change=='curve':pump['accounts'][10]=address()
    elif change=='owner':pump['accounts'][13]=address()
    elif change=='ata':pump['accounts'][14]=address()
    elif change=='quote':swap['accounts'][6]=address()
    elif change=='direction':swap['accounts'][4],swap['accounts'][5]=swap['accounts'][5],swap['accounts'][4]
    elif change=='duplicate':ixs.insert(6,deepcopy(swap))
    elif change=='outflow':
        for b in raw['meta']['postTokenBalances']:
            if b['mint']==pump['accounts'][1]:b['uiTokenAmount']['amount']='0'
    assert decode(Tx(raw)) is None


def test_buy_v2_has_same_route_accounts():
    raw=sample();ix=raw['transaction']['message']['instructions'][7]
    ix['data']=b58encode(discriminator('global','buy_v2')+(1000).to_bytes(8,'little')+(100).to_bytes(8,'little'))
    assert decode(Tx(raw))


class RPC:
    def __init__(self,store=None,oid=None,send_error=False,simulation_error=False):
        self.calls=[];self.store=store;self.oid=oid;self.send_error=send_error;self.simulation_error=simulation_error

    async def call(self,method,params):
        self.calls.append(method)
        if method=='simulateTransaction':return {'value':{'err':'failed' if self.simulation_error else None}}
        if method=='sendTransaction':
            row=self.store.order(self.oid)
            assert row['state']=='signed' and row['raw']==params[0] and row['signature']
            if self.send_error:raise TimeoutError('ambiguous-send')
            return row['signature']
        if method=='getSignatureStatuses':return {'value':[None]}
        if method=='getBlockHeight':return 200
        raise AssertionError(method)


def setup_order(config,store,keypair=None):
    route=fresh_route()
    wallet=str(keypair.pubkey()) if keypair else route.trade.wallet
    cfg=replace(config,wallet_address=wallet,telegram_token='',telegram_chat='')
    oid=store.reserve(cfg,route.trade.mint,route.trade.pool,cfg.buy_amount)
    ix=transfer(TransferParams(from_pubkey=pubkey(wallet),to_pubkey=Keypair().pubkey(),lamports=cfg.buy_amount))
    message=MessageV0.try_compile(pubkey(wallet),[ix],[],Hash.default())
    unsigned=VersionedTransaction.populate(message,[Signature.default()])
    result=dict(transaction=base64.b64encode(bytes(unsigned)).decode(),lastHeight=100,
                wallet=wallet,mint=route.trade.mint,amount=str(cfg.buy_amount),quotedOut='1000',minOut='980',quoteIn='100')
    async def builder(*args):return result
    return cfg,route,oid,builder


@pytest.mark.asyncio
async def test_dry_simulates_without_key_or_broadcast(config,store):
    cfg,route,oid,builder=setup_order(config,store)
    cfg=replace(cfg,wallet_file=Path('does-not-exist'))
    rpc=RPC();engine=Executor(cfg,store,rpc,Notices(),Priority(),builder)
    await engine.buy(oid,route)
    assert rpc.calls==['simulateTransaction']
    assert store.order(oid)['state']=='dry-simulated'
    assert not store.rows('trading','SELECT * FROM positions')


@pytest.mark.asyncio
async def test_simulation_failure_never_signs_or_sends(config,store):
    cfg,route,oid,builder=setup_order(config,store)
    rpc=RPC(simulation_error=True)
    await Executor(cfg,store,rpc,Notices(),Priority(),builder).buy(oid,route)
    assert store.order(oid)['state']=='failed'
    assert rpc.calls==['simulateTransaction']


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous',[False,True])
async def test_live_journals_before_send_and_retains_unknown(config,store,tmp_path,ambiguous):
    key=Keypair();path=tmp_path/'test-key.json';path.write_text(json.dumps(list(bytes(key))))
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_file=path),store,key)
    rpc=RPC(store,oid,send_error=ambiguous);engine=Executor(cfg,store,rpc,Notices(),Priority(),builder)
    await engine.buy(oid,route)
    assert store.order(oid)['state']==('unknown' if ambiguous else 'submitted')
    assert store.order(oid)['reason']==MARKER
    await engine.reconcile_once()
    assert store.order(oid)['state']=='unknown'
    assert store.reserve(cfg,route.trade.mint,route.trade.pool,cfg.buy_amount) is None
    await engine.buy(oid,route)
    assert rpc.calls.count('sendTransaction')==1


@pytest.mark.asyncio
async def test_expired_signal_never_builds(config,store):
    cfg,route,oid,builder=setup_order(config,store)
    route=replace(route,trade=replace(route.trade,time=int(time.time())-100))
    async def forbidden(*args):raise AssertionError('must not build')
    rpc=RPC()
    await Executor(cfg,store,rpc,Notices(),Priority(),forbidden).buy(oid,route)
    assert store.order(oid)['state']=='failed' and not rpc.calls


@pytest.mark.asyncio
async def test_hotlist_buy_signal_reaches_executor(config,store,monkeypatch):
    raw=sample();raw['blockTime']=int(time.time());route=decode(Tx(raw))
    cfg=replace(config,n=1)
    f=Funding('fund','fund',route.trade.wallet,address(),'cex:test',10**9,route.trade.slot-1,raw['blockTime']-10)
    store.admit(f,3600,time.time());store.enqueue([route.trade.signature])
    service=Service(cfg,store);service.signals.started=raw['blockTime']-1
    calls=[]
    class Engine:
        async def buy(self,oid,r):calls.append((oid,r))
    class TransactionRPC:
        async def transaction(self,sig):return raw
    service.executor=Engine()
    async def cached_limit(route):return {"quoteLimit":str(route.observed_amount)}
    monkeypatch.setattr(service.quote_builder,"quote_limit",cached_limit)
    await service.process({'signature':route.trade.signature},TransactionRPC())
    await service.process({'signature':route.trade.signature},TransactionRPC())
    assert len(calls)==1 and calls[0][1].dlmm_pool==route.dlmm_pool


@pytest.mark.asyncio
@pytest.mark.parametrize('failed',[False,True])
async def test_finalized_result_updates_position_once(config,store,tmp_path,failed):
    key=Keypair();path=tmp_path/'test-key.json';path.write_text(json.dumps(list(bytes(key))))
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_file=path),store,key)
    rpc=RPC(store,oid);engine=Executor(cfg,store,rpc,Notices(),Priority(),builder)
    await engine.buy(oid,route)
    signature=store.order(oid)['signature']
    raw=sample()
    original_wallet=route.trade.wallet
    raw=json.loads(json.dumps(raw).replace(original_wallet,cfg.wallet_address))
    raw['transaction']['signatures'][0]=signature
    class FinalizedRPC:
        async def call(self,method,params):
            assert method=='getSignatureStatuses'
            return {'value':[{'confirmationStatus':'finalized','err':'failed' if failed else None}]}
        async def transaction(self,sig,commitment):
            assert sig==signature and commitment=='finalized'
            return raw
    restarted=Executor(cfg,store,FinalizedRPC(),Notices(),Priority(),builder)
    await restarted.reconcile_once();await restarted.reconcile_once()
    if failed:
        assert store.order(oid)['state']=='failed'
        assert not store.rows('trading','SELECT * FROM positions')
    else:
        assert store.order(oid)['state']=='finalized'
        rows=store.rows('trading','SELECT * FROM positions')
        assert len(rows)==1 and int(rows[0]['amount'])==route.trade.tokens


def test_restart_expires_own_unsigned_orders_only(config,store):
    cfg,route,oid,_=setup_order(config,store)
    legacy=store.reserve(cfg,address(),address(),cfg.buy_amount)
    store.update_order(oid,reason=MARKER)
    Executor(cfg,store,None,Notices(),Priority()).recover_unsigned()
    assert store.order(oid)['state']=='failed'
    assert store.order(legacy)['state']=='reserved'


@pytest.mark.asyncio
async def test_wallet_mismatch_never_broadcasts(config,store,tmp_path):
    path=tmp_path/'test-key.json';path.write_text(json.dumps(list(bytes(Keypair()))))
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_file=path),store,Keypair())
    rpc=RPC(store,oid)
    await Executor(cfg,store,rpc,Notices(),Priority(),builder).buy(oid,route)
    assert rpc.calls==['simulateTransaction']
    assert store.order(oid)['state']=='failed' and store.order(oid)['signature'] is None
