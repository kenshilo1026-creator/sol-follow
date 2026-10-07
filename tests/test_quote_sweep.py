import asyncio
import base64
import json
import time
from dataclasses import replace
import pytest
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import transfer,TransferParams
from solders.transaction import VersionedTransaction
from chain_common.primitives import TOKEN,WSOL
from chain_common.rpc import Priority
from trade_execution.quote_sweep import QuoteSweep,enqueue
from tests.helpers import address,Notices,transaction


def setup(config,store,mint=None):
    key=Keypair();wallet=str(key.pubkey());mint=mint or address()
    cfg=replace(config,dry_run=False,wallet_keypair=key,wallet_address=wallet)
    recipe={'version':1,'tables':[],'steps':[{'label':'Whirlpool','pool':address(),'inputMint':WSOL,'outputMint':mint}]}
    with store.db('trading') as c:enqueue(c,'first',wallet,mint,TOKEN,1000,10,recipe)
    message=MessageV0.try_compile(key.pubkey(),[transfer(TransferParams(from_pubkey=key.pubkey(),
        to_pubkey=Keypair().pubkey(),lamports=1))],[],Hash.new_unique())
    unsigned=VersionedTransaction.populate(message,[Signature.default()])
    result={'transaction':base64.b64encode(bytes(unsigned)).decode(),'side':'sweep','wallet':wallet,'mint':mint,
            'amount':'1000','lastHeight':100,'quotedOut':'10000','minOut':'9800','quoteTime':time.time()*1000,
            'risk':{'feePpm':'10000','impactPpm':'1000'}}
    class Rpc:
        def __init__(self):self.calls=[];self.status=None;self.height=20;self.ambiguous=False
        async def call(self,method,params):
            self.calls.append(method)
            if method=='simulateTransaction':return {'value':{'err':None}}
            if method=='sendTransaction':
                row=store.rows('trading',"SELECT * FROM quote_sweeps WHERE id='first'")[0]
                assert row['raw']==params[0] and row['signature']
                if self.ambiguous:raise TimeoutError('unknown')
                return row['signature']
            if method=='getSignatureStatuses':return {'value':[self.status]}
            if method=='getBlockHeight':return self.height
            raise AssertionError(method)
        async def transaction(self,sig,commitment=None):
            def balance(n):return [{'accountIndex':1,'mint':mint,'owner':wallet,'programId':TOKEN,'uiTokenAmount':{'amount':str(n)}}]
            return transaction([wallet,address()],{wallet},[],sig=sig,slot=20,when=int(time.time()),
                pre=[1000000,2000000],post=[1005000,2000000],pre_tokens=balance(2000),post_tokens=balance(1000))
    calls=[]
    async def builder(*args):calls.append(args);return result
    engine=QuoteSweep(cfg,store,Rpc(),Notices(),Priority(),builder)
    return engine,result,calls


@pytest.mark.asyncio
async def test_only_runs_idle_and_preserves_existing_balance(config,store):
    engine,result,calls=setup(config,store)
    engine.priority.active=1
    await engine.once();assert not calls
    engine.priority.active=0
    oid=store.reserve(engine.config,address(),address(),100)
    await engine.once();assert not calls
    store.fail_order(oid,'test')
    await engine.once()
    assert len(calls)==1 and calls[0][-1]==1000
    assert store.rows('trading','SELECT state FROM quote_sweeps')[0]['state']=='submitted'
    engine.rpc.status={'confirmationStatus':'finalized','err':None}
    await engine.once();await engine.once()
    assert store.rows('trading','SELECT state FROM quote_sweeps')[0]['state']=='finalized'
    assert len(calls)==1


@pytest.mark.asyncio
async def test_new_buy_cancels_background_build(config,store):
    engine,result,calls=setup(config,store)
    started=asyncio.Event();cancelled=asyncio.Event()
    async def build(*args):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    engine.builder=build
    task=asyncio.create_task(engine.once());await started.wait()
    engine.priority.active=1
    await asyncio.wait_for(task,1)
    assert cancelled.is_set() and not engine.rpc.calls
    assert store.rows('trading','SELECT signature FROM quote_sweeps')[0]['signature'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['fee','impact','expired'])
async def test_limits_reject_before_signing(config,store,fault):
    engine,result,calls=setup(config,store)
    if fault=='fee':result['risk']['feePpm']=str(engine.config.max_total_fee_bps*100+1)
    if fault=='impact':result['risk']['impactPpm']=str(engine.config.max_price_impact_bps*100+1)
    if fault=='expired':result['quoteTime']-=6000
    await engine.once()
    assert 'sendTransaction' not in engine.rpc.calls
    assert store.rows('trading','SELECT signature FROM quote_sweeps')[0]['signature'] is None


@pytest.mark.asyncio
async def test_unknown_signature_blocks_other_sweeps_and_survives_restart(config,store):
    engine,result,calls=setup(config,store)
    engine.rpc.ambiguous=True
    await engine.once()
    signature=store.rows('trading','SELECT signature FROM quote_sweeps')[0]['signature']
    with store.db('trading') as c:enqueue(c,'second',engine.config.wallet_address,address(),TOKEN,1000,10,None)
    restart=QuoteSweep(engine.config,store,engine.rpc,engine.notices,engine.priority,engine.builder)
    engine.rpc.height=101
    await restart.once()
    assert len(calls)==1
    assert store.rows('trading',"SELECT signature FROM quote_sweeps WHERE id='first'")[0]['signature']==signature
    assert store.rows('trading',"SELECT state FROM quote_sweeps WHERE id='second'")[0]['state']=='waiting'


@pytest.mark.asyncio
async def test_dry_never_builds_or_sends(config,store):
    engine,result,calls=setup(config,store)
    engine.config=replace(engine.config,dry_run=True)
    await engine.once()
    assert not calls and not engine.rpc.calls


@pytest.mark.asyncio
async def test_pending_exit_blocks_sweep_even_without_active_builder(config,store):
    engine,result,calls=setup(config,store)
    oid=store.reserve(engine.config,address(),address(),100)
    store.update_order(oid,state='finalized')
    with store.db('trading') as c:
        c.execute("INSERT INTO dev_exits(buy_id,state,updated) VALUES (?,'waiting',?)",(oid,time.time()))
    await engine.once();assert not calls
    with store.db('trading') as c:c.execute("UPDATE dev_exits SET state='finalized'")
    await engine.once();assert len(calls)==1


@pytest.mark.asyncio
async def test_proven_failure_can_retry_but_confirmed_cannot(config,store):
    engine,result,calls=setup(config,store)
    await engine.once()
    engine.rpc.status={'confirmationStatus':'confirmed','err':None}
    await engine.once();assert len(calls)==1
    engine.rpc.status={'confirmationStatus':'finalized','err':{'InstructionError':[0,'test']}}
    await engine.once()
    row=store.rows('trading','SELECT * FROM quote_sweeps')[0]
    assert row['state']=='waiting' and row['signature'] is None


@pytest.mark.asyncio
async def test_fee_limit_equality_passes(config,store):
    engine,result,calls=setup(config,store)
    result['risk']['feePpm']=str(engine.config.max_total_fee_bps*100)
    await engine.once()
    assert 'sendTransaction' in engine.rpc.calls


@pytest.mark.asyncio
async def test_intermediate_gain_is_queued_once_not_existing_balance(config,store):
    engine,result,calls=setup(config,store)
    mid=address();wallet=engine.config.wallet_address
    recipe={'version':1,'tables':[],'steps':[
        {'label':'Whirlpool','pool':address(),'inputMint':WSOL,'outputMint':mid},
        {'label':'Whirlpool','pool':address(),'inputMint':mid,'outputMint':result['mint']}]}
    engine.update('first',recipe=json.dumps(recipe))
    await engine.once()
    original=engine.rpc.transaction
    async def receipt(*args,**kwargs):
        raw=await original(*args,**kwargs)
        raw['transaction']['message']['accountKeys'].append({'pubkey':address(),'signer':False,'writable':True})
        for phase,amount in [('pre',500),('post',550)]:
            raw['meta'][phase+'Balances'].append(2000000)
            raw['meta'][phase+'TokenBalances'].append({'accountIndex':2,'mint':mid,'owner':wallet,
                'programId':TOKEN,'uiTokenAmount':{'amount':str(amount)}})
        return raw
    engine.rpc.transaction=receipt;engine.rpc.status={'confirmationStatus':'finalized','err':None}
    await engine.once()
    child=store.rows('trading','SELECT * FROM quote_sweeps WHERE mint=?',(mid,))[0]
    assert child['amount']=='50' and len(json.loads(child['recipe'])['steps'])==1
