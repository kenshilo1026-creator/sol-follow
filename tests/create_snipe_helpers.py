"""Offline service-level create scenarios shared by strategy migration tests."""
from dataclasses import replace
from types import SimpleNamespace
import time

from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.funding.decoder import Funding
from features.runtime.service import Service
from launchpads.create import decode
from tests.helpers import Notices,address
from tests.test_create_decoders import pump,launchlab
from features.strategy.dev_create import create_route


def setup_create(config,store,*,launchpad='pump',non_sol=False,processed=True,live=True,fail=False):
    quote=address() if non_sol else WSOL
    raw=launchlab(quote=quote) if launchpad=='stonk' else pump(quote=quote if non_sol else None)
    now=int(time.time());raw['blockTime']=now
    tx=Tx(raw);item=decode(tx)[0]
    store.admit(Funding('create-funding','fund',item.user,address(),'cex:test',10**9,
                        item.slot-1,now-10),3600,time.time())
    cfg=replace(config,hotlist_commitment='processed' if processed else 'confirmed')
    service=Service(cfg,store);service.signals.started=now-1;service.notices=Notices()
    calls=[]
    class Engine:
        async def buy(self,oid,route):
            calls.append((oid,route))
            if fail:store.fail_order(oid,'simulation-test-failure')
    class RPC:
        async def transaction(self,sig):return raw
        async def call(self,*args):raise AssertionError('unexpected create scenario RPC')
    service.executor=Engine()
    if processed:
        raw['blockTime']=None;raw['_stream']={'commitment':'processed','live':live}
        store.stream_transaction(raw)
    else:store.enqueue([item.signature],live=live)
    if live:store.mark_live(item.signature)
    return SimpleNamespace(service=service,raw=raw,item=item,calls=calls,rpc=RPC(),
                           row={'signature':item.signature},route=create_route(item,tx))



def token_snapshot(raw,item,amount=0):
    """Synthetic successful fresh-mint balances, including its pool vault."""
    from chain_common.primitives import ata,pda,pubkey
    from launchpads import stonk
    vault=(pda([b'pool_vault',bytes(pubkey(item.pool)),bytes(pubkey(item.mint))],stonk.PROGRAM)
           if item.launchpad==stonk.NAME else ata(item.pool,item.mint,item.base_token_program))
    keys=raw['transaction']['message']['accountKeys']
    def index(account):
        for i,k in enumerate(keys):
            if k['pubkey']==account:return i
        keys.append({'pubkey':account,'signer':False,'writable':True,'source':'transaction'})
        raw['meta']['preBalances'].append(0);raw['meta']['postBalances'].append(0)
        return len(keys)-1
    def row(account,owner,value):
        return {'accountIndex':index(account),'mint':item.mint,'owner':owner,'programId':item.base_token_program,
                'uiTokenAmount':{'amount':str(value),'decimals':item.decimals}}
    raw['meta']['preTokenBalances']=[]
    raw['meta']['postTokenBalances']=[row(vault,item.pool,10**15-amount)]
    if amount:raw['meta']['postTokenBalances'].append(row(ata(item.user,item.mint,item.base_token_program),item.user,amount))
    return raw
