import asyncio
import base64
from dataclasses import replace
import json
import pytest
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction
from chain_common.primitives import pubkey
from chain_common.rpc import Priority
from trade_execution.executor import Executor
from trade_execution.simulation import reusable
from tests.helpers import Notices
from tests.test_pump_buy import setup_order, RPC


def wire(wallet,receiver,amount,units,blockhash=None):
    ixs=[set_compute_unit_limit(units),transfer(TransferParams(
        from_pubkey=pubkey(wallet),to_pubkey=receiver,lamports=amount))]
    msg=MessageV0.try_compile(pubkey(wallet),ixs,[],blockhash or Hash.default())
    tx=VersionedTransaction.populate(msg,[Signature.default()])
    return tx,base64.b64encode(bytes(tx)).decode()


async def fixture(config,store):
    key=Keypair()
    cfg,route,oid,build=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    result=await build()
    receiver=Keypair().pubkey()
    tx,encoded=wire(cfg.wallet_address,receiver,cfg.buy_amount,48000)
    _,simulated=wire(cfg.wallet_address,receiver,cfg.buy_amount,1400000)
    result.update(transaction=encoded,units=48000,simulation={
        'version':1,'transaction':simulated,'slot':route.trade.slot,
        'commitment':cfg.hotlist_commitment,'unitsConsumed':40000,'err':None})
    return cfg,route,oid,result,tx,receiver


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    None,'missing','stale-slot','commitment','err','missing-err','wire','units','version',
    'limit','blockhash','instructions','signatures',
])
async def test_only_matching_successful_sdk_simulation_is_reused(config,store,mutation):
    cfg,route,oid,result,tx,receiver=await fixture(config,store)
    p=result['simulation']
    if mutation=='missing':result.pop('simulation')
    elif mutation=='stale-slot':p['slot']-=1
    elif mutation=='commitment':p['commitment']='invalid'
    elif mutation=='err':p['err']='failed'
    elif mutation=='missing-err':p.pop('err')
    elif mutation=='wire':p['transaction']='bad base64'
    elif mutation=='units':p['unitsConsumed']=1400001
    elif mutation=='version':p['version']=True
    elif mutation=='limit':tx,result['transaction']=wire(cfg.wallet_address,receiver,cfg.buy_amount,40000)
    elif mutation=='blockhash':tx,result['transaction']=wire(cfg.wallet_address,receiver,cfg.buy_amount,48000,Hash.new_unique())
    elif mutation=='instructions':tx,result['transaction']=wire(cfg.wallet_address,receiver,cfg.buy_amount+1,48000)
    elif mutation=='signatures':tx=VersionedTransaction(tx.message,[cfg.wallet_keypair])
    assert reusable(result,tx,slot=route.trade.slot,commitment=cfg.hotlist_commitment)==(mutation is None)


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence',['valid','missing','mismatch'])
async def test_live_buy_simulates_once_then_sends_without_preflight(config,store,evidence):
    cfg,route,oid,result,_,_=await fixture(config,store)
    if evidence=='missing':result.pop('simulation')
    if evidence=='mismatch':result['simulation']['slot']-=1
    async def build(*args):return result
    class Public(RPC):
        async def call(self,method,params):
            if method=='sendTransaction':
                assert params[1]['skipPreflight'] is True
                assert params[1]['minContextSlot']==route.trade.slot
            return await super().call(method,params)
    rpc=Public(store,oid)
    await Executor(cfg,store,rpc,Notices(),Priority(),build).buy(oid,route)
    assert rpc.calls==(['sendTransaction'] if evidence=='valid' else ['simulateTransaction','sendTransaction'])
    assert store.order(oid)['state']=='submitted'
    row=store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]
    detail=json.loads(row['detail'])
    assert detail['simulation_source']==('sdk-reused' if evidence=='valid' else 'python-fallback')
    assert detail['timing_ms']['submit_ack_ms']>=detail['timing_ms']['submit_started_ms']


@pytest.mark.asyncio
async def test_reused_simulation_still_waits_for_monitor_and_rechecks_freshness(config,store):
    cfg,route,oid,result,_,_=await fixture(config,store)
    built=asyncio.Event();release=asyncio.Event()
    async def build(*args):built.set();return result
    async def monitor():await release.wait();return True
    ready=asyncio.create_task(monitor())
    rpc=RPC(store,oid);engine=Executor(cfg,store,rpc,Notices(),Priority(),build)
    task=asyncio.create_task(engine.buy(oid,route,readiness=ready))
    try:
        await asyncio.wait_for(built.wait(),2)
        assert not rpc.calls and store.order(oid)['signature'] is None
        engine.config=replace(cfg,signal_age=-1)
        release.set();await asyncio.wait_for(task,2)
        assert store.order(oid)['state']=='failed' and not rpc.calls
    finally:
        for t in (ready,task):
            if not t.done():t.cancel()
        await asyncio.gather(ready,task,return_exceptions=True)


@pytest.mark.asyncio
async def test_reused_simulation_dry_run_never_signs_or_sends(config,store):
    cfg,route,oid,result,_,_=await fixture(config,store)
    cfg=replace(cfg,dry_run=True)
    with store.db('trading') as db:db.execute("UPDATE orders SET mode='dry' WHERE id=?",(oid,))
    async def build(*args):return result
    rpc=RPC()
    await Executor(cfg,store,rpc,Notices(),Priority(),build).buy(oid,route)
    assert not rpc.calls and store.order(oid)['state']=='dry-simulated'
    assert store.order(oid)['raw'] is None


@pytest.mark.asyncio
async def test_stream_commit_wakes_waiting_worker_without_poll_delay(config,store,monkeypatch):
    from features.runtime.service import Service
    from tests.test_pump_buy import sample
    service=Service(config,store);waiting=asyncio.Event();processed=asyncio.Event()
    wait=store.stream_ready.wait
    async def waiting_for_event():waiting.set();return await wait()
    async def process(row,rpc):
        assert store.stream_payload(row['signature']) is not None
        processed.set();raise asyncio.CancelledError()
    monkeypatch.setattr(store.stream_ready,'wait',waiting_for_event)
    monkeypatch.setattr(service,'process',process)
    task=asyncio.create_task(service.worker(None,streamed=True))
    try:
        await asyncio.wait_for(waiting.wait(),2)
        raw=sample();store.stream_transaction(raw)
        assert store.stream_ready.is_set()
        await asyncio.wait_for(processed.wait(),2)
        with pytest.raises(asyncio.CancelledError):await task
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


def test_failed_stream_commit_never_wakes_worker(config,store,monkeypatch):
    from contextlib import contextmanager
    from tests.test_pump_buy import sample
    original=store.db
    @contextmanager
    def broken(name):
        with original(name) as db:
            class Fail:
                def __getattr__(self,name):return getattr(db,name)
                def commit(self):raise RuntimeError('disk-failure')
            yield Fail()
    monkeypatch.setattr(store,'db',broken)
    with pytest.raises(RuntimeError,match='disk-failure'):store.stream_transaction(sample())
    assert not store.stream_ready.is_set()


@pytest.mark.parametrize('fee',[None,'200000','0'])
def test_real_node_attestation_is_accepted_by_python(config,fee):
    import subprocess
    program=r"""
const web3=require('@solana/web3.js');
const {finish}=require('./trade_execution/sdk/build.cjs');
(async()=>{
  const user=web3.Keypair.generate().publicKey;
  const conn={getLatestBlockhash:async()=>({blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:300}),
    simulateTransaction:async()=>({context:{slot:101},value:{err:null,unitsConsumed:89980}})};
  const ix=web3.SystemProgram.transfer({fromPubkey:user,toPubkey:web3.Keypair.generate().publicKey,lamports:1000});
  process.stdout.write(JSON.stringify(await finish(conn,{lookupTables:[],minSlot:100,commitment:'processed',...JSON.parse(process.argv[1])},user,[ix])));
})().catch(()=>{process.exitCode=1;});
"""
    output=subprocess.run(['node','-e',program,json.dumps({} if fee is None else {'priorityFeeLamports':fee})],cwd=config.root,check=True,capture_output=True,text=True,timeout=30)
    result=json.loads(output.stdout)
    tx=VersionedTransaction.from_bytes(base64.b64decode(result['transaction']))
    assert reusable(result,tx,slot=100,commitment='processed',priority_fee_lamports=None if fee is None else int(fee))


@pytest.mark.parametrize('mutation',[None,'price','limit','swap','budget','charged','simulation-price'])
def test_priority_fee_simulation_only_reuses_budget_bound_compute_changes(mutation):
    from solders.compute_budget import set_compute_unit_price
    key=Keypair();receiver=Keypair().pubkey();budget=200000;units=113352
    def packet(limit,price,amount=1):
        msg=MessageV0.try_compile(key.pubkey(),[set_compute_unit_limit(limit),set_compute_unit_price(price),
            transfer(TransferParams(from_pubkey=key.pubkey(),to_pubkey=receiver,lamports=amount))],[],Hash.default())
        tx=VersionedTransaction.populate(msg,[Signature.default()])
        return tx,base64.b64encode(bytes(tx)).decode()
    price=budget*10**6//units
    simulated_price=((budget-1)*10**6+1+1399999)//1400000
    _,simulated=packet(1400000,simulated_price+(1 if mutation=='simulation-price' else 0))
    tx,_=packet(units+(1 if mutation=='limit' else 0),price+(1 if mutation=='price' else 0),2 if mutation=='swap' else 1)
    result={'units':units,'priorityFee':{'budgetLamports':str(budget+(1 if mutation=='budget' else 0)),
        'microLamports':str(price),'chargedLamports':str(budget+(1 if mutation=='charged' else 0))},
        'simulation':{'version':2,'transaction':simulated,'slot':100,'commitment':'processed','err':None,'unitsConsumed':94460}}
    assert reusable(result,tx,slot=100,commitment='processed',priority_fee_lamports=budget)==(mutation is None)
