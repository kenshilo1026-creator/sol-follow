from dataclasses import replace
import json
from pathlib import Path
import pytest
from chain_common.transaction import Tx,Unsupported
from chain_common.primitives import SYSTEM
from features.funding.decoder import decode as funding
from tests.helpers import address,transaction


def test_batch_funding(config):
    cex,a,b=address(),address(),address()
    def ix(dest):return {'programId':SYSTEM,'parsed':{'type':'transfer','info':{'source':cex,'destination':dest,'lamports':1000000000}}}
    r=transaction([cex,a,b],[cex],[ix(a)],pre=[3*10**9,0,0],post=[10**9-5000,10**9,10**9],
                  inner=[{'index':0,'instructions':[ix(b)]}])
    result=funding(Tx(r),replace(config,cex={cex:'test'}))
    assert {f.wallet for f in result}=={a,b}
    assert len({f.event for f in result})==2
    r['transaction']['message']['accountKeys'][0]['signer']=False
    assert funding(Tx(r),replace(config,cex={cex:'test'}))==[]


def test_privacy_real_fixture(config):
    r=json.loads((Path(__file__).parent/'fixtures/privacy_cash_native.json').read_text())
    result=funding(Tx(r),config)
    assert len(result)==1
    assert result[0].provider=='privacy-cash'
    assert result[0].amount==29889000000
    assert result[0].wallet=='5Sob2NjhNetRPv8TCKhnbRPnkSdTm6LoFcaa196gAGwC'
    r['meta']['postBalances'][r['transaction']['message']['accountKeys'].index(next(k for k in r['transaction']['message']['accountKeys'] if k['pubkey']==result[0].wallet))]+=1
    assert funding(Tx(r),config)==[]


@pytest.mark.parametrize('version',['legacy',0,1])
def test_supported_transaction_versions(version):
    raw=transaction([address()],[],[])
    raw['version']=version
    assert Tx(raw).slot==100


@pytest.mark.parametrize('mutation',['failed','missing-time','version','keys'])
def test_bad_transaction(mutation):
    raw=transaction([address()],[],[])
    if mutation=='failed':raw['meta']['err']={'InstructionError':[0,'custom']}
    if mutation=='missing-time':raw['blockTime']=None
    if mutation=='version':raw['version']=2
    if mutation=='keys':raw['meta']['postBalances'].pop()
    with pytest.raises(Unsupported):Tx(raw)
