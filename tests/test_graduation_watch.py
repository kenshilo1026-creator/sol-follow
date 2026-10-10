import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from chain_common.primitives import SYSTEM, WSOL, USDC, ata, pubkey, b58encode, discriminator
from chain_common.transaction import Tx
from features.audit.graduation_watch import (Archive, Reads, Unknown, log_hints, migration_instructions,
    verify_migration, find_creation, admission, creation_buy, analyse, export, main)
from launchpads import pump_fun, stonk
from launchpads.create import inspect
from tests.helpers import address, transaction

FIXTURES=Path(__file__).parent/'fixtures'
SAMPLES=json.loads((FIXTURES/'create_samples.json').read_text())


@pytest.mark.parametrize('sample',SAMPLES,ids=lambda r:r['mint'][:6])
def test_real_create_logs_produce_pool_hint_only_for_emitting_program(sample):
    raw=json.loads((FIXTURES/Path(sample['fixture']).name).read_text())
    created=inspect(Tx(raw)).creates[0]
    assert ('create',created.pool) in log_hints(created.program,raw['meta']['logMessages'])
    other=stonk.PROGRAM if created.program==pump_fun.PROGRAM else pump_fun.PROGRAM
    assert log_hints(other,raw['meta']['logMessages'])==[]


@pytest.mark.parametrize('ending',['failed: custom program error: 1',None])
def test_failed_or_truncated_invocations_do_not_produce_graduation_hint(ending):
    logs=[f'Program {pump_fun.PROGRAM} invoke [1]','Program log: Instruction: Migrate']
    if ending:logs.append(f'Program {pump_fun.PROGRAM} {ending}')
    assert log_hints(pump_fun.PROGRAM,logs)==[]


def test_foreign_program_cannot_spoof_migration_or_failed_parent():
    other=address()
    assert log_hints(pump_fun.PROGRAM,[f'Program {other} invoke [1]',
        'Program log: Instruction: Migrate',f'Program {other} success'])==[]
    assert log_hints(pump_fun.PROGRAM,[f'Program {other} invoke [1]',f'Program {pump_fun.PROGRAM} invoke [2]',
        'Program log: Instruction: MigrateV2',f'Program {pump_fun.PROGRAM} success',f'Program {other} failed: bad'])==[]


@pytest.mark.parametrize('kind',['migrate','migrate_v2','migrate_to_cpswap','migrate_to_amm'])
def test_graduation_instruction_layout_and_origin(kind):
    mint,quote=address(),address()
    is_pump=kind in ('migrate','migrate_v2')
    program=pump_fun.PROGRAM if is_pump else stonk.PROGRAM
    if kind=='migrate':quote=WSOL
    pool=pump_fun.curve_address(mint) if is_pump else stonk.pool_address(mint,quote)
    a=[address() for _ in range(32)]
    if is_pump:
        a[2]=mint;a[3 if kind=='migrate' else 4]=pool
        if kind=='migrate_v2':a[3]=quote
    else:
        a[1:3]=[mint,quote];a[17 if kind=='migrate_to_cpswap' else 23]=pool;a[3]=stonk.PLATFORM
    payload=discriminator('global',kind)+(bytes(17) if kind=='migrate_to_amm' else b'')
    raw=transaction(list(dict.fromkeys([*a,program])),[],[{'programId':program,'accounts':a,'data':b58encode(payload)}])
    item,=migration_instructions(Tx(raw))
    assert (item['mint'],item['quote_mint'],item['pool'])==(mint,quote,pool)
    data=bytearray(429 if not is_pump else 141)
    data[:8]=discriminator('account','BondingCurve' if is_pump else 'PoolState')
    if is_pump:data[48]=1
    else:
        data[17]=2;data[173:205]=bytes(pubkey(stonk.PLATFORM));data[205:237]=bytes(pubkey(mint));data[237:269]=bytes(pubkey(quote))
    snapshot={'context':{'slot':100},'value':{'owner':program,'executable':False,'data':[base64.b64encode(data).decode(),'base64']}}
    assert verify_migration(item,snapshot)
    if not is_pump:
        data[173:205]=bytes(pubkey(address()))
        snapshot['value']['data'][0]=base64.b64encode(data).decode()
        assert verify_migration(item,snapshot) is False
    snapshot['context']['slot']=99
    with pytest.raises(Unknown,match='stale'):verify_migration(item,snapshot)


def test_real_graduated_pool_account_status_offset():
    data=json.loads((FIXTURES/'graduated_cpmm_accounts.json').read_text())
    pool='58HsRLZ4Xcksd2CNRJYyrszFX6YswUSAyDYZFudKguoC'
    item={'launchpad':'stonk','pool':pool,'mint':'7y4NAbrNJbc1xQjtsZoNrhoKwJjBTxG8euKPTiX5nC1w',
          'quote_mint':'Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8','slot':454149770}
    assert verify_migration(item,{'context':data['context'],'value':data['accounts'][pool]})


@pytest.fixture
def original():
    raw=json.loads((FIXTURES/'create_GcHEn2AHcGDc4Dbhx3bgVotqcrtnvmDwgtfSuT5ytCj.json').read_text())
    return inspect(Tx(raw)).creates[0],Tx(raw)


class ResearchRPC:
    def __init__(self,created,config,*,active=False):
        self.created=created;self.cex=next(iter(config.cex));self.active=active;self.calls=[]
        wallet=created.creator
        self.funding=transaction([self.cex,wallet],[self.cex],[{'programId':SYSTEM,'parsed':{'type':'transfer',
            'info':{'source':self.cex,'destination':wallet,'lamports':1_000_000_000}}}],
            pre=[2_000_000_000,0],post=[999995000,1_000_000_000],slot=created.slot-10,when=created.time-20,sig='funding')

    async def call(self,method,params):
        self.calls.append((method,params))
        if method=='getAccountInfo':return {'context':{'slot':self.created.slot},'value':{'owner':SYSTEM,'data':['','base64'],'executable':False}}
        if method=='getTokenAccountsByOwner':return {'value':[]}
        assert method=='getSignaturesForAddress'
        if params[0]==ata(self.created.creator,USDC):return []
        assert params[0]==self.created.creator
        rows=[{'signature':'funding','slot':self.created.slot-10,'blockTime':self.created.time-20,'err':None}]
        if self.active:rows.append({'signature':'old-signed','slot':self.created.slot-20,'blockTime':self.created.time-30,'err':None})
        return rows

    async def transaction(self,sig):
        if sig=='funding':return self.funding
        assert sig=='old-signed'
        return transaction([self.created.creator],[self.created.creator],[],slot=self.created.slot-20,
                           when=self.created.time-30,sig=sig)


@pytest.mark.asyncio
@pytest.mark.parametrize('active',[False,True])
async def test_theoretical_admission_allows_recent_signed_activity_without_live_bot(tmp_path,config,original,active):
    created,_=original;cfg=replace(config,cex={address():'test'},min_funding=1,max_funding=2_000_000_000)
    rpc=ResearchRPC(created,cfg,active=active);archive=Archive(tmp_path)
    result=await admission(created,rpc,archive,cfg,3)
    assert result['status']=='pass' and result['funding']['signature']=='funding'
    assert not list(tmp_path.glob('trading*'))
    assert {m for m,_ in rpc.calls} <= {'getAccountInfo','getTokenAccountsByOwner','getSignaturesForAddress'}


@pytest.mark.asyncio
async def test_missing_historical_account_is_unknown_not_blocked(tmp_path,config,original):
    created,_=original
    class RPC:
        async def call(self,*args):return {'value':None}
    assert (await admission(created,RPC(),Archive(tmp_path),config,3))['status']=='unknown'


def test_create_decodable_is_not_automatically_a_buy(config,original):
    created,tx=original
    assert creation_buy(created,tx,replace(config,max_observed_buy=5_000_000_000))['status']=='pass'
    assert creation_buy(created,tx,replace(config,max_observed_buy=4_000_000_000))['status']=='blocked'
    assert creation_buy(replace(created,creator=address()),tx,config)['reason']=='no-supported-dev-buy-in-create-tx'
    raw=json.loads((FIXTURES/'create_4Nr7xPstQ6VT9MF1kKwmkRy3b1mXct9UPsNgs53Cpump.json').read_text())
    token_tx=Tx(raw);token_create=inspect(token_tx).creates[0]
    assert creation_buy(token_create,token_tx,config)['status']=='unknown'


@pytest.mark.asyncio
async def test_cached_create_hint_verified_and_full_analysis(tmp_path,config,original):
    created,tx=original;archive=Archive(tmp_path)
    cfg=replace(config,cex={address():'test'},max_observed_buy=5_000_000_000)
    class RPC(ResearchRPC):
        async def transaction(self,sig):
            if sig==created.signature:return tx.raw
            return await super().transaction(sig)
    rpc=RPC(created,cfg)
    with archive.db() as c:c.execute('INSERT INTO hints VALUES (?,?)',(created.pool,created.signature))
    item={'event':'graduation:0','mint':created.mint,'pool':created.pool,'launchpad':created.launchpad,
          'slot':created.slot+1,'signature':'graduation','time':created.time+1}
    result=await analyse(item,rpc,archive,cfg,3)
    assert result['candidate']=='pass' and result['execution']=='not-simulated-not-an-order'
    assert result['dev']==created.creator and 'other-hotlist-wallet-votes' in result['unchecked_execution_gates']
    archive.save_result(result)
    archive.enqueue('graduation',created.slot+1);archive.enqueue('graduation',created.slot+1)
    report=export(archive)
    assert report['graduated_tokens']==1 and report['queue']==[{'state':'pending','n':1}]
    assert (tmp_path/'report.csv').read_text(encoding='utf-8-sig').count(created.mint)==1
    restarted=Archive(tmp_path,hours=1)
    assert restarted.end==archive.end
    main(['report','--output',str(tmp_path)])


@pytest.mark.asyncio
async def test_query_budget_and_no_write_rpc(tmp_path):
    class RPC:
        async def call(self,method,params):return 'result'
    reads=Reads(RPC(),Archive(tmp_path),rps=1000,budget=1)
    with pytest.raises(ValueError,match='read-only'):await reads.call('sendTransaction',[])
    assert await reads.call('getGenesisHash',[])=='result'
    with pytest.raises(Unknown,match='budget-exhausted'):await reads.call('getAccountInfo',[])


@pytest.mark.asyncio
async def test_create_hint_cannot_substitute_different_token(tmp_path,original):
    created,tx=original;archive=Archive(tmp_path)
    with archive.db() as c:c.execute('INSERT INTO hints VALUES (?,?)',(created.pool,created.signature))
    class RPC:
        async def transaction(self,sig):return tx.raw
    with pytest.raises(Unknown,match='original-create'):
        await find_creation({'pool':created.pool,'mint':address(),'launchpad':created.launchpad,'slot':created.slot+1},RPC(),archive,3)


@pytest.mark.asyncio
async def test_continuous_watch_manual_stop_saves_and_cancels_workers(tmp_path,config,monkeypatch):
    import features.audit.graduation_watch as module
    archive=Archive(tmp_path)
    assert archive.end is None
    started=asyncio.Event();cancelled=[]
    class RPC:
        def __init__(self,*args,**kwargs):pass
        async def call(self,method,params):
            assert method=='getGenesisHash'
            return '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
    async def background(*args):
        assert args[-1]==float('inf')
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.append(True)
    monkeypatch.setattr(module,'Rpc',RPC)
    monkeypatch.setattr(module,'websocket',background)
    monkeypatch.setattr(module,'worker',background)
    args=SimpleNamespace(grace_seconds=0,rps=.5,max_rpc_per_token=120,pages=3)
    task=asyncio.create_task(module.watch(args,archive,config))
    try:
        await asyncio.wait_for(started.wait(),3)
        main(['stop','--output',str(tmp_path)])
        await asyncio.wait_for(task,3)
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    report=json.loads((tmp_path/'report.json').read_text())
    assert report['end'] is None and len(cancelled)==2
    assert report['coverage'][-1]['kind']=='stopped' and report['coverage'][-1]['detail']=='manual-stop'


def test_default_watch_converts_existing_fixed_window_without_losing_data(tmp_path,config,monkeypatch):
    import features.audit.graduation_watch as module
    archive=Archive(tmp_path,hours=24);start=archive.start
    archive.enqueue('saved-job',123)
    (tmp_path/'STOP').touch()
    async def no_network(args,resumed,cfg):
        assert resumed.start==start and resumed.end is None
        assert not (tmp_path/'STOP').exists()
        assert resumed.rows('','SELECT signature FROM queue')==[{'signature':'saved-job'}]
    monkeypatch.setattr(module,'watch',no_network)
    monkeypatch.setattr(module,'load',lambda **kwargs:config)
    main(['watch','--output',str(tmp_path)])
    assert Archive(tmp_path).end is None
