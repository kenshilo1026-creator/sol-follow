from dataclasses import replace
import sqlite3
import time
import pytest
from features.funding.discovery import history_page
from features.database.maintenance import batch,Maintenance
from features.database.storage import Store
from features.funding.decoder import Funding
from features.runtime.service import Service
from chain_common.primitives import SYSTEM
from chain_common.rpc import Priority
from tests.helpers import address
from tests.helpers import Notices


@pytest.mark.asyncio
async def test_gap_checkpoint_survives_rpc_failure(store):
    now=int(time.time());addr=address()
    first=[{'signature':str(i),'err':None,'blockTime':now} for i in range(100)]
    class RPC:
        async def call(self,method,params):
            if 'before' in params[1]:raise RuntimeError('429')
            return first
    await history_page(RPC(),store,addr,3600,Notices())
    with pytest.raises(RuntimeError):await history_page(RPC(),store,addr,3600,Notices())
    state=store.cursor(addr)
    assert state['latest']=='0' and state['gaps'][0]['before']=='99'
    assert len(store.rows('funding','SELECT * FROM jobs'))==100
    class Recover:
        def __init__(self):self.calls=[]
        async def call(self,method,params):
            self.calls.append(params[1])
            if 'before' not in params[1]:return []
            return [{'signature':'older','err':None,'blockTime':now}]
    rpc=Recover()
    await history_page(rpc,store,addr,3600,Notices())
    assert store.cursor(addr)['gaps']==[]
    assert rpc.calls[0]['before']=='99'


def test_persistent_jobs_and_original_ttl(config,store):
    now=time.time();w=address()
    f=Funding('event','sig',w,address(),'cex:test',10**9,1,int(now))
    assert store.admit(f,1000,now)
    assert not store.admit(f,1000,now+10)
    assert store.hotlist(now)[w]['expires']==int(now)+1000
    store.enqueue(['pending'],now)
    restarted=Store(config.data)
    assert restarted.due(1,now)[0]['signature']=='pending'


def test_bounded_cleanup_preserves_active(config,store):
    now=time.time();w=address();f=Funding('e','s',w,address(),'cex:test',1,1,int(now))
    store.admit(f,1000,now)
    store.enqueue(['pending'],now-100000)
    store.enqueue(['done'],now-100000);store.job_result('done','done')
    assert batch(store,'funding','jobs',"state='done' AND first_seen<?",(now,))==1
    assert store.hotlist(now)[w]
    assert store.rows('funding','SELECT signature FROM jobs')==[{'signature':'pending'}]


@pytest.mark.asyncio
async def test_maintenance_yields_to_buying(config,store):
    store.audit('old',{})
    priority=Priority();priority.active=1
    m=Maintenance(replace(config,audit_retention=-1),store,priority,Notices())
    assert await m.pass_once()==0
    assert len(store.rows('audit','SELECT * FROM audit'))==1


@pytest.mark.asyncio
async def test_wallet_qualification_checks_owner(config,store):
    now=int(time.time());w=address();f=Funding('event','sig',w,address(),'cex:test',1,1,now)
    class RPC:
        async def call(self,method,params):
            if method=='getSignaturesForAddress':
                return [{'signature':f.signature,'slot':f.slot,'blockTime':f.time}]
            return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
    service=Service(config,store)
    assert await service.qualify(f,RPC())
    class WrongOwner:
        async def call(self,*args):return {'value':{'owner':address(),'executable':False,'data':['','base64']}}
    assert not await service.qualify(f,WrongOwner())
