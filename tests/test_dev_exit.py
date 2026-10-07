"""Offline races and durable emergency exits; synthetic keys and RPC only."""
import base64
import json
import time
from dataclasses import replace
import pytest
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction
from chain_common.rpc import Priority
from features.database.storage import Store
from trade_execution.dev_exit import DevExit
from trade_execution.executor import Executor
from tests.helpers import Notices, address, transaction
from tests.test_pump_buy import setup_order


def blocked(store,mint):
    with store.db('trading') as c:
        c.execute("INSERT OR REPLACE INTO token_dev_holdings VALUES (?,?,?,?,?,'blocked','{}')",
                  (mint,'source',address(),1,time.time()))


def receipt(wallet,mint,quote,signature,sold=False):
    target,out=address(),address()
    def balance(index,mint,amount):
        return {'accountIndex':index,'mint':mint,'owner':wallet,'uiTokenAmount':{'amount':str(amount)}}
    return transaction([wallet,target,out],{wallet},[],sig=signature,when=int(time.time()),
        pre_tokens=[balance(1,mint,5000 if sold else 4000),balance(2,quote,500)],
        post_tokens=[balance(1,mint,4000 if sold else 5000),balance(2,quote,1500 if sold else 500)])


async def setup(config,store,send_error=False):
    key=Keypair()
    cfg,route,oid,old_builder=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    built=await old_builder()
    unsigned=VersionedTransaction.from_bytes(base64.b64decode(built['transaction']))
    signed=VersionedTransaction(unsigned.message,[key]);buy_sig=str(signed.signatures[0])
    store.update_order(oid,state='submitted',signature=buy_sig,raw=base64.b64encode(bytes(signed)).decode(),last_height=100)
    with store.db('trading') as c:c.execute('INSERT INTO order_routes VALUES (?,?)',(oid,json.dumps(route.request())))
    blocked(store,route.trade.mint)
    class Rpc:
        calls=[];status=None;height=20;indexed=True;simulation_error=False
        async def transaction(self,sig,commitment=None):
            self.calls.append(('transaction',commitment))
            if sig==buy_sig:
                return receipt(cfg.wallet_address,route.trade.mint,route.quote_mint,sig) if self.indexed else None
            return receipt(cfg.wallet_address,route.trade.mint,route.quote_mint,sig,True)
        async def call(self,method,params):
            self.calls.append((method,params))
            if method=='simulateTransaction':return {'value':{'err':'bad' if self.simulation_error else None}}
            if method=='getSignatureStatuses':return {'value':[self.status]}
            if method=='getBlockHeight':return self.height
            if method=='sendTransaction':
                saved=store.rows('trading','SELECT * FROM dev_exits WHERE buy_id=?',(oid,))[0]
                assert saved['signature'] and saved['raw']==params[0]
                if send_error:raise TimeoutError('secret endpoint')
                return saved['signature']
            raise AssertionError(method)
    rpc=Rpc();built_count=[]
    async def builder(config,request,wallet,amount):
        built_count.append(amount)
        message=MessageV0.try_compile(key.pubkey(),[transfer(TransferParams(
            from_pubkey=key.pubkey(),to_pubkey=Keypair().pubkey(),lamports=1))],[],Hash.new_unique())
        transaction=VersionedTransaction.populate(message,[Signature.default()])
        return {**built,'transaction':base64.b64encode(bytes(transaction)).decode(),
                'side':'sell','amount':str(amount),'quoteMint':route.quote_mint}
    engine=DevExit(cfg,store,rpc,Notices(),Priority(),builder)
    return engine,rpc,oid,route,built_count


@pytest.mark.asyncio
@pytest.mark.parametrize('buy_finalized_first',[True,False])
async def test_late_result_sells_only_bought_amount_and_reconciles_either_order(config,store,buy_finalized_first):
    engine,rpc,oid,route,builds=await setup(config,store)
    if buy_finalized_first:store.fill(oid,1000,100)
    await engine.once()
    job=store.rows('trading','SELECT * FROM dev_exits')[0]
    assert job['state']=='submitted' and job['amount']=='1000'
    assert builds==[1000]  # Preserve the wallet's pre-existing 4000 tokens.
    rpc.status={'confirmationStatus':'finalized','err':None}
    await engine.once()
    if not buy_finalized_first:store.fill(oid,1000,100)
    assert store.rows('trading','SELECT amount FROM positions')[0]['amount']=='0'
    await engine.once()
    assert builds==[1000] and sum(x[0]=='sendTransaction' for x in rpc.calls)==1


@pytest.mark.asyncio
async def test_unknown_sell_survives_restart_without_new_signature(config,store):
    engine,rpc,oid,route,builds=await setup(config,store,send_error=True)
    await engine.once()
    job=store.rows('trading','SELECT * FROM dev_exits')[0]
    assert job['state']=='unknown'
    restart=DevExit(engine.config,Store(config.data),rpc,Notices(),Priority(),engine.builder)
    rpc.height=101
    await restart.once()
    assert builds==[1000]
    assert store.rows('trading','SELECT * FROM dev_exits')[0]['signature']==job['signature']
    assert store.rows('trading','SELECT * FROM dev_exits')[0]['state']=='unknown'


@pytest.mark.asyncio
async def test_waits_for_own_buy_to_land(config,store):
    engine,rpc,oid,route,builds=await setup(config,store)
    rpc.indexed=False
    await engine.once()
    assert not builds
    rpc.indexed=True
    await engine.once()
    assert builds==[1000]


@pytest.mark.asyncio
async def test_proven_sell_failure_allows_retry_but_not_pending_status(config,store):
    engine,rpc,oid,route,builds=await setup(config,store)
    await engine.once()
    rpc.status={'confirmationStatus':'confirmed','err':None}
    await engine.once();assert builds==[1000]
    rpc.status={'confirmationStatus':'finalized','err':{'InstructionError':[0,'error']}}
    await engine.once()
    assert store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='waiting'
    engine.next.clear();await engine.once()
    assert builds==[1000,1000]


@pytest.mark.asyncio
async def test_simulation_failure_never_signs_or_sends(config,store):
    engine,rpc,oid,route,builds=await setup(config,store)
    rpc.simulation_error=True
    await engine.once()
    assert not store.rows('trading','SELECT signature FROM dev_exits')[0]['signature']
    assert not any(x[0]=='sendTransaction' for x in rpc.calls)


@pytest.mark.asyncio
async def test_dry_exit_and_unavailable_do_not_sell(config,store):
    engine,rpc,oid,route,builds=await setup(config,store)
    with store.db('trading') as c:c.execute("UPDATE token_dev_holdings SET state='unavailable'")
    await engine.once();assert not builds and not store.rows('trading','SELECT * FROM dev_exits')
    blocked(store,route.trade.mint)
    with store.db('trading') as c:c.execute("UPDATE orders SET mode='dry',state='dry-simulated'")
    engine.config=replace(engine.config,dry_run=True)
    await engine.once()
    assert not builds and store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='dry-skipped'


@pytest.mark.asyncio
async def test_result_arrives_during_build_prevents_buy_send(config,store):
    key=Keypair()
    cfg,route,oid,build=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    async def racing(*args):
        blocked(store,route.trade.mint)
        return await build()
    class Rpc:
        async def call(self,*args):raise AssertionError('must stop before simulate or send')
    await Executor(cfg,store,Rpc(),Notices(),Priority(),racing).buy(oid,route)
    assert store.order(oid)['state']=='failed' and store.order(oid)['signature'] is None
