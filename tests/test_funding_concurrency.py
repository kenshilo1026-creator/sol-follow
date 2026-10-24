"""Offline concurrency barriers: capacity, leases, wallet isolation and RPC budgets."""
import asyncio
import time
from dataclasses import replace

import pytest

from chain_common.background_rpc import BackgroundRpc
from chain_common.rpc import Priority, RpcError
from features.funding.rpc import FundingRpc
from features.funding.work import TransactionReads
from features.funding.decoder import Funding
from features.funding.activity_filter import qualify_activity
from features.runtime.service import Service
from share_common.config import load
from tests.helpers import address, transaction


@pytest.mark.asyncio
async def test_http_overlaps_with_bounded_capacity_and_paced_starts():
    starts=[]
    both=asyncio.Event()
    release=asyncio.Event()
    class Client:
        async def call(self, method, params):
            starts.append(time.monotonic())
            if len(starts)==2:both.set()
            await release.wait()
            return 1
    rpc=BackgroundRpc(Client(),Priority(),100,max_inflight=2)
    tasks=[asyncio.create_task(rpc.call('getBlockTime',[n])) for n in range(3)]
    try:
        await asyncio.wait_for(both.wait(),1)
        assert len(starts)==2  # Second HTTP began before the first returned.
        assert starts[1]-starts[0]>=0.009
        release.set()
        assert await asyncio.gather(*tasks)==[1,1,1]
        assert starts[2]-starts[1]>=0.009
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
async def test_public_failure_does_not_serialize_alchemy_or_repeat_public_wave():
    started=asyncio.Event();release=asyncio.Event()
    class Public:
        calls=0
        async def call(self,*args):
            self.calls+=1
            await asyncio.sleep(0)
            raise RpcError('HTTP-429')
    class Alchemy:
        calls=0
        async def call(self,*args):
            self.calls+=1
            if self.calls==4:started.set()
            await release.wait()
            return 1000
    public=Public();alchemy=Alchemy();rpc=FundingRpc(public,alchemy)
    tasks=[asyncio.create_task(rpc.call('getBlockTime',[n])) for n in range(4)]
    try:
        await asyncio.wait_for(started.wait(),1)
        assert public.calls==1
        release.set()
        assert await asyncio.gather(*tasks)==[1000]*4
        assert rpc.snapshot()['recovered']==4
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
async def test_old_success_cannot_clear_new_failure_cooldown():
    started=asyncio.Event();release=asyncio.Event()
    class Alchemy:
        calls=0
        async def call(self,method,params):
            self.calls+=1
            if params==['slow']:
                started.set();await release.wait();return 1000
            raise RpcError('HTTP-429')
    alchemy=Alchemy();rpc=FundingRpc(None,alchemy,clock=lambda:100)
    slow=asyncio.create_task(rpc.call('getBlockTime',['slow']))
    try:
        await asyncio.wait_for(started.wait(),1)
        with pytest.raises(RpcError):await rpc.call('getBlockTime',['fail'])
        release.set();assert await slow==1000
        assert rpc.cooldown['alchemy']==130
        with pytest.raises(RpcError):await rpc.call('getBlockTime',['cooling'])
        assert alchemy.calls==2
    finally:
        slow.cancel();await asyncio.gather(slow,return_exceptions=True)


@pytest.mark.asyncio
async def test_four_workers_overlap_and_expired_lease_cannot_duplicate_active_job(config,store,monkeypatch):
    service=Service(config,store);seen=[];four=asyncio.Event();five=asyncio.Event()
    store.enqueue([f'job-{n}' for n in range(5)],live=True)
    async def process(row,rpc):
        seen.append(row['signature'])
        if len(seen)==4:four.set()
        if len(seen)==5:five.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service,'process',process)
    tasks=[asyncio.create_task(service.worker(None,False)) for _ in range(4)]
    try:
        await asyncio.wait_for(four.wait(),1)
        assert len(service.active_jobs)==4
        # Simulate a slow job crossing its durable lease, without sleeping a minute.
        with store.db('funding') as db:db.execute('UPDATE jobs SET due=0')
        tasks.append(asyncio.create_task(service.worker(None,False)))
        await asyncio.wait_for(five.wait(),1)
        assert len(set(seen))==5
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    assert not service.active_jobs
    assert len(store.rows('funding',"SELECT * FROM jobs WHERE state='pending'"))==5


@pytest.mark.asyncio
async def test_same_wallet_serializes_other_wallet_progresses_and_cancel_cleans(config,store,monkeypatch):
    service=Service(config,store);entered=[];first=asyncio.Event();other=asyncio.Event();release=asyncio.Event()
    item=Funding('event','fund',address(),address(),'cex:test',10**9,1000,int(time.time()))
    async def qualify(candidate,rpc):
        entered.append(candidate.event)
        if candidate.event=='event':first.set();await release.wait()
        if candidate.event=='other':other.set()
        return True
    monkeypatch.setattr(service,'_qualify',qualify)
    tasks=[asyncio.create_task(service.qualify(item,None))]
    try:
        await asyncio.wait_for(first.wait(),1)
        tasks.extend([asyncio.create_task(service.qualify(replace(item,event='same'),None)),
                      asyncio.create_task(service.qualify(replace(item,event='other',wallet=address()),None))])
        await asyncio.wait_for(other.wait(),1)
        assert entered==['event','other']
        tasks[1].cancel()
        await asyncio.gather(tasks[1],return_exceptions=True)
        release.set();await asyncio.gather(tasks[0],tasks[2])
        assert not service.funding_flights.entries
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
async def test_activity_reuses_deposit_but_still_downloads_prior_transactions(store):
    item=Funding('event','fund',address(),address(),'cex:test',10**9,1000,int(time.time()))
    raw=transaction([item.wallet],[],[],sig='fund',slot=1000,when=item.time)
    old=transaction([item.wallet],[],[],sig='old',slot=999,when=item.time-1)
    class Client:
        downloaded=[]
        async def call(self,method,params):
            assert method=='getSignaturesForAddress'
            return [dict(signature='fund',slot=1000,blockTime=item.time,err=None),
                    dict(signature='old',slot=999,blockTime=item.time-1,err=None)]
        async def transaction(self,sig):
            self.downloaded.append(sig)
            assert sig=='old'
            return old
    client=Client()
    assert await qualify_activity(item,TransactionReads(client,raw),store)==(True,'history-qualified')
    assert client.downloaded==['old']


def test_parallel_config_defaults_and_overrides():
    config=load(env={})
    assert (config.funding_workers,config.funding_alchemy_rps,config.history_rps)==(4,8,2)
    custom=load(env={'SOL_FUNDING_WORKERS':'2','SOL_FUNDING_ALCHEMY_RPS':'5'})
    assert (custom.funding_workers,custom.funding_alchemy_rps)==(2,5)


@pytest.mark.parametrize('key,value',[('SOL_FUNDING_WORKERS','0'),('SOL_FUNDING_WORKERS','9'),
                                    ('SOL_FUNDING_ALCHEMY_RPS','0'),('SOL_FUNDING_ALCHEMY_RPS','21')])
def test_parallel_config_bounds(key,value):
    with pytest.raises(ValueError):load(env={key:value})
