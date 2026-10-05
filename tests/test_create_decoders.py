import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
import struct
import time
from types import SimpleNamespace

import pytest
from chain_common.primitives import SYSTEM,TOKEN,ATA,WSOL,pda,pubkey,b58encode,b58decode
from chain_common.transaction import Tx,Unsupported
from features.database.maintenance import batch
from features.runtime.service import Service
from launchpads import pump_fun,stonk
from launchpads.create import decode,inspect
from launchpads.create_pump import CREATE,CREATE_V2,TOKEN_2022,METADATA,RENT,MAYHEM,associated
from launchpads.create_stonk import INITIALIZE,INITIALIZE_V2,INITIALIZE_2022
from tests.helpers import address,transaction,Notices

FIXTURES=Path(__file__).parent/'fixtures'
SAMPLES=json.loads((FIXTURES/'create_samples.json').read_text())
EXPECTED={
    'CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump':('pump.fun',WSOL,'Backers','BACKERS'),
    '49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6':('stonk','Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh','Super Model','SM'),
    'GcHEn2AHcGDc4Dbhx3bgVotqcrtnvmDwgtfSuT5ytCj':('stonk',WSOL,'Human Inu','HI'),
    '4Nr7xPstQ6VT9MF1kKwmkRy3b1mXct9UPsNgs53Cpump':('pump.fun','SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb','Elonius Maximus','EM'),
}
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def sample(row):return json.loads((Path(__file__).parent.parent/row['fixture']).read_text())


def strings(*values):
    return b''.join(struct.pack('<I',len(v.encode()))+v.encode() for v in values)


def assemble(program,a,data,user,mint,*,cpi=False,version=0):
    wrapper=address()
    keys=list(dict.fromkeys([*a,program,*([wrapper] if cpi else [])]))
    ix={'programId':program,'accounts':a,'data':b58encode(data),'stackHeight':2 if cpi else 1}
    if cpi:
        raw=transaction(keys,[user,mint],[{'programId':wrapper,'accounts':[],'data':''}],
                        inner=[{'index':0,'instructions':[ix]}])
    else:raw=transaction(keys,[user,mint],[ix])
    raw['version']=version
    return raw


def pump(v2=True,quote=None,quote_program=TOKEN,tail=b'\x00'+bytes(8)+b'\x00',cpi=False):
    mint,user,creator=address(),address(),address()
    curve=pump_fun.curve_address(mint)
    base=TOKEN_2022 if v2 else TOKEN
    a=[mint,pda([b'mint-authority'],pump_fun.PROGRAM),curve,associated(curve,mint,base),pda([b'global'],pump_fun.PROGRAM)]
    if v2:
        state=pda([b'mayhem-state',bytes(pubkey(mint))],MAYHEM)
        a += [user,SYSTEM,TOKEN_2022,ATA,MAYHEM,pda([b'global-params'],MAYHEM),pda([b'sol-vault'],MAYHEM),state,address(),pda([b'__event_authority'],pump_fun.PROGRAM),pump_fun.PROGRAM]
        if quote:a += [quote,associated(curve,quote,quote_program),quote_program]
    else:
        a += [METADATA,pda([b'metadata',bytes(pubkey(METADATA)),bytes(pubkey(mint))],METADATA),user,SYSTEM,TOKEN,ATA,RENT,pda([b'__event_authority'],pump_fun.PROGRAM),pump_fun.PROGRAM]
    data=(CREATE_V2 if v2 else CREATE)+strings('測試','TEST','https://example.invalid/metadata')+bytes(pubkey(creator))
    if v2:data+=b'\x00'+tail
    return assemble(pump_fun.PROGRAM,a,data,user,mint,cpi=cpi)


def launchlab(kind='2022',quote=WSOL,quote_program=TOKEN,curve=0,optional='none',cpi=False):
    mint,user,creator,config=address(),address(),address(),address()
    pool=stonk.pool_address(mint,quote)
    a=[user,creator,config,stonk.PLATFORM,pda([b'vault_auth_seed'],stonk.PROGRAM),pool,mint,quote,
       pda([b'pool_vault',bytes(pubkey(pool)),bytes(pubkey(mint))],stonk.PROGRAM),
       pda([b'pool_vault',bytes(pubkey(pool)),bytes(pubkey(quote))],stonk.PROGRAM)]
    if kind=='2022':a += [TOKEN_2022,quote_program,SYSTEM,pda([b'__event_authority'],stonk.PROGRAM),stonk.PROGRAM]
    else:a += [pda([b'metadata',bytes(pubkey(METADATA)),bytes(pubkey(mint))],METADATA),TOKEN,quote_program,METADATA,SYSTEM,RENT,pda([b'__event_authority'],stonk.PROGRAM),stonk.PROGRAM]
    if optional in ('allow','both'):a += [pda([b'platform_allow_config',bytes(pubkey(stonk.PLATFORM)),bytes(pubkey(config))],stonk.PROGRAM)]
    if optional in ('rule','both'):a += [pda([b'platform_curve_rule',bytes(pubkey(stonk.PLATFORM)),bytes(pubkey(config))],stonk.PROGRAM)]
    disc={'legacy':INITIALIZE,'v2':INITIALIZE_V2,'2022':INITIALIZE_2022}[kind]
    data=disc+b'\x06'+strings('測試','TEST','https://example.invalid/metadata')+bytes([curve])+struct.pack('<Q',10**15)
    if curve==0:data+=struct.pack('<Q',793100000000000)
    data+=struct.pack('<QBQQQ',85000000000,1,0,0,0)
    if kind!='legacy':data+=b'\x00'
    if kind=='2022':data+=b'\x01'+struct.pack('<HQ',100,10**15)
    return assemble(stonk.PROGRAM,a,data,user,mint,cpi=cpi)


@pytest.mark.parametrize('row',SAMPLES,ids=lambda r:r['mint'][:6])
def test_four_real_create_transactions(row):
    tx=Tx(sample(row));result=inspect(tx)
    assert not result.rejected and len(result.creates)==1
    created=result.creates[0]
    assert (created.launchpad,created.quote_mint,created.name,created.symbol)==EXPECTED[row['mint']]
    assert created.mint==row['mint'] and created.signature==row['signature'] and created.slot==row['slot']
    assert created.decimals==6 and created.base_token_program==TOKEN_2022
    assert created.quote_kind==('sol' if created.quote_mint==WSOL else 'token')
    assert created.uri.startswith('https://') and created.creator and created.user


def test_observed_pump_suffix_is_preserved_without_guessing_flags():
    row=next(r for r in SAMPLES if r['mint'].startswith('4Nr7'))
    flags=decode(Tx(sample(row)))[0].flags
    assert flags['uninterpreted_tail_hex']=='01'
    assert flags['creator_fee_bps'] is None and flags['holder_reward'] is None


@pytest.mark.parametrize('v2,quote,program',[(False,None,TOKEN),(True,None,TOKEN),(True,USDC,TOKEN),(True,address(),TOKEN_2022)])
def test_pump_other_mints_and_quote_programs(v2,quote,program):
    tx=Tx(pump(v2,quote,program));created=decode(tx)[0]
    assert created.mint not in EXPECTED
    assert created.quote_mint==(quote or WSOL)
    assert created.quote_token_program==program and created.name=='測試'


@pytest.mark.parametrize('tail',[b'',b'\x00',b'\x00\x01',b'\x00'+struct.pack('<Q',250),b'\x00'+struct.pack('<Q',250)+b'\x01'])
def test_pump_optional_tail_versions(tail):
    assert len(decode(Tx(pump(tail=tail))))==1


@pytest.mark.parametrize('kind,quote,program',[('legacy',WSOL,TOKEN),('legacy',USDC,TOKEN),('v2',WSOL,TOKEN),('v2',address(),TOKEN_2022),('2022',WSOL,TOKEN),('2022',USDC,TOKEN),('2022',address(),TOKEN_2022)])
@pytest.mark.parametrize('curve',[0,1,2])
def test_launchlab_abi_and_curve_variants(kind,quote,program,curve):
    result=inspect(Tx(launchlab(kind,quote,program,curve)))
    assert not result.rejected and len(result.creates)==1
    created=result.creates[0]
    assert created.mint not in EXPECTED and created.quote_mint==quote
    assert created.flags['curve_kind']==curve
    assert created.creator!=created.user # don't silently substitute the fee payer


@pytest.mark.parametrize('optional',['none','allow','rule','both'])
def test_launchlab_optional_accounts(optional):
    assert len(decode(Tx(launchlab(optional=optional))))==1


@pytest.mark.parametrize('with_struct',[False,True])
def test_launchlab_without_transfer_fee_extension(with_struct):
    raw=launchlab();ix=raw['transaction']['message']['instructions'][0]
    data=b58decode(ix['data'])[:-11]+b'\x00'+(bytes(10) if with_struct else b'')
    ix['data']=b58encode(data)
    created=decode(Tx(raw))[0]
    assert created.flags['transfer_fee_enabled'] is False
    assert created.flags['transfer_fee_bps']==0


@pytest.mark.parametrize('factory',[pump,launchlab])
def test_cpi_and_indexed_accounts(factory):
    raw=factory(cpi=True)
    ix=raw['meta']['innerInstructions'][0]['instructions'][0]
    keys=[k['pubkey'] for k in raw['transaction']['message']['accountKeys']]
    ix['accounts']=[keys.index(k) for k in ix['accounts']]
    created=decode(Tx(raw))[0]
    assert created.instruction_path=='0.0'
    # Successful invoke_signed may have a PDA signer absent from outer signatures.
    for key in raw['transaction']['message']['accountKeys']:key['signer']=False
    assert len(decode(Tx(raw)))==1
    ix['stackHeight']=None
    assert not decode(Tx(raw))


@pytest.mark.parametrize('factory',[pump,launchlab])
@pytest.mark.parametrize('bad',['program','signer','accounts','index','data','utf8','oversize','truncated'])
def test_malformed_or_wrong_instruction_is_not_admitted(factory,bad):
    raw=factory();ix=raw['transaction']['message']['instructions'][0]
    if bad=='program':ix['programId']=address()
    if bad=='signer':
        for key in raw['transaction']['message']['accountKeys']:key['signer']=False
    if bad=='accounts':ix['accounts'].pop()
    if bad=='index':ix['accounts'][0]=-1
    if bad=='data':ix['data']='0!'
    if bad in ('utf8','oversize','truncated'):
        data=bytearray(b58decode(ix['data']))
        offset=9 if factory==launchlab else 8
        if bad=='oversize':data[offset:offset+4]=struct.pack('<I',2**32-1)
        if bad=='utf8':data[offset+4]=255
        if bad=='truncated':data=data[:offset+2]
        ix['data']=b58encode(bytes(data))
    assert not decode(Tx(raw))


@pytest.mark.parametrize('row',SAMPLES,ids=lambda r:r['mint'][:6])
@pytest.mark.parametrize('bad',['pool','vault','quote','failed'])
def test_real_sample_account_proofs_reject_tampering(row,bad):
    raw=sample(row);created=decode(Tx(raw))[0]
    ix=next(ix for path,ix in Tx(raw).instructions() if path==created.instruction_path)
    if bad=='failed':
        raw['meta']['err']={'InstructionError':[0,'custom']}
        with pytest.raises(Unsupported):Tx(raw)
        return
    index=2 if created.launchpad=='pump.fun' else 5
    if bad=='vault':index=3 if created.launchpad=='pump.fun' else 8
    if bad=='quote':
        if created.launchpad=='pump.fun' and created.quote_kind=='sol':
            ix['accounts'] += [address()]
            assert not decode(Tx(raw));return
        index=16 if created.launchpad=='pump.fun' else 7
    original=ix['accounts'][index]
    replacement=address()
    for key in raw['transaction']['message']['accountKeys']:
        if key['pubkey']==original:key['pubkey']=replacement
    for _,instruction in Tx(raw).instructions():
        if 'accounts' in instruction:
            instruction['accounts']=[replacement if key==original else key for key in instruction['accounts']]
    result=inspect(Tx(raw))
    assert not result.creates and result.rejected
    assert result.rejected[0]['reason']=='create-account-proof'


def test_unrelated_launchlab_platform_is_not_stonk():
    raw=launchlab();ix=raw['transaction']['message']['instructions'][0]
    ix['accounts'][3]=ix['accounts'][2]
    result=inspect(Tx(raw))
    assert not result.creates and not result.rejected


def test_unknown_program_instruction_is_not_create():
    raw=pump();ix=raw['transaction']['message']['instructions'][0]
    ix['data']=b58encode(b'UNKNOWN!'+bytes(64))
    result=inspect(Tx(raw))
    assert not result.creates and not result.rejected


@pytest.mark.parametrize('row',SAMPLES,ids=lambda r:r['mint'][:6])
@pytest.mark.asyncio
async def test_service_records_create_once_without_votes_or_orders(row,config,store):
    raw=sample(row);raw['blockTime']=int(time.time())
    class RPC:
        async def transaction(self,*args):return raw
        async def call(self,*args):raise AssertionError('Create decode requires no new RPC or trade')
    service=Service(config,store);service.notices=Notices()
    store.enqueue([row['signature']])
    for _ in range(2):await service.process({'signature':row['signature']},RPC())
    creates=store.rows('funding','SELECT * FROM launch_creates')
    assert len(creates)==1 and creates[0]['mint']==row['mint'] and creates[0]['status']=='confirmed'
    assert len([n for n in service.notices.items if n[0][0]=='launchpad-create'])==1
    assert not store.rows('trading','SELECT * FROM votes')
    assert not store.rows('trading','SELECT * FROM orders')
    assert store.rows('funding','SELECT signature FROM chain_checks')==[{'signature':row['signature']}]


def test_create_finality_and_cleanup_preserve_pending_evidence(store):
    created=decode(Tx(pump()))[0]
    assert store.record_creates([created])==[created]
    assert store.record_creates([created])==[]
    assert batch(store,'funding','launch_creates',"status IN ('finalized','invalid') AND time<?",(10**12,))==0
    store.finalize_creates(created.signature)
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'finalized'}]
    store.invalidate(created.signature)
    store.finalize_creates(created.signature)
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'invalid'}]
    assert batch(store,'funding','launch_creates',"status IN ('finalized','invalid') AND time<? AND signature NOT IN (SELECT signature FROM chain_checks)",(10**12,))==1


@pytest.mark.parametrize('error',[None,{'InstructionError':[0,'custom']}])
@pytest.mark.asyncio
async def test_service_finalized_updates_create_status(error,config,store,monkeypatch):
    created=decode(Tx(pump()))[0]
    store.record_creates([created])
    class RPC:
        async def call(self,method,params):
            assert method=='getSignatureStatuses'
            return {'value':[{'confirmationStatus':'finalized','err':error}]}
    async def stop(_):raise asyncio.CancelledError()
    monkeypatch.setattr('features.runtime.service.asyncio.sleep',stop)
    with pytest.raises(asyncio.CancelledError):await Service(config,store).finalized(RPC())
    assert store.rows('funding','SELECT status FROM launch_creates')==[{'status':'invalid' if error else 'finalized'}]
    assert not store.rows('funding','SELECT * FROM chain_checks')


def test_multiple_creates_in_one_signature_are_distinct(store):
    first,second=pump(),pump(quote=USDC)
    keys=list({k['pubkey']:k for k in first['transaction']['message']['accountKeys']+second['transaction']['message']['accountKeys']}.values())
    raw=transaction([k['pubkey'] for k in keys],[k['pubkey'] for k in keys if k['signer']],
                    first['transaction']['message']['instructions']+second['transaction']['message']['instructions'])
    creates=decode(Tx(raw))
    assert len(creates)==2 and len({c.event for c in creates})==2
    assert len(store.record_creates(creates))==2
    assert not store.record_creates(creates)


@pytest.mark.parametrize('row',SAMPLES,ids=lambda r:r['mint'][:6])
@pytest.mark.asyncio
async def test_probe_outputs_real_creates(row,config,monkeypatch,capsys):
    import features.probe as module
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class RPC:
        async def transaction(self,signature,commitment):
            assert signature==row['signature'] and commitment=='finalized'
            return sample(row)
    monkeypatch.setattr(module,'load',lambda:config)
    monkeypatch.setattr(module.aiohttp,'ClientSession',Session)
    monkeypatch.setattr(module,'Rpc',lambda *a,**k:RPC())
    await module.probe(SimpleNamespace(mint=None,tx=row['signature'],raw=False,buy_route=False))
    output=json.loads(capsys.readouterr().out)
    assert output['creates'][0]['mint']==row['mint'] and output['create_rejections']==[]
    assert output['trades']==[]
