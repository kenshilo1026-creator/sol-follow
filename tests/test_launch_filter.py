"""Admission must reject prior supported launches, including dormant creators."""
from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest
from chain_common.primitives import SYSTEM
from chain_common.transaction import Tx
from features.funding.activity_filter import qualify_activity, HistoryPending
from features.runtime.service import Service
from launchpads.create import inspect
from tests.helpers import address
from tests.test_activity_filter import deposit, row, RPC
from tests.test_create_decoders import SAMPLES, sample, pump, launchlab


class HistoryRPC(RPC):
    def __init__(self,item,entries,details):
        super().__init__(item,{None:entries})
        self.details=details
    async def call(self,method,params):
        if method=='getAccountInfo':
            return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
        return await super().call(method,params)
    async def transaction(self,sig):
        if sig not in self.details:return await super().transaction(sig)
        self.calls.append(('transaction',sig))
        return self.details[sig]


def history(deposit,raw,*,wallet=None,count=2,position='old'):
    raw=deepcopy(raw)
    created=inspect(Tx(raw)).creates[0]
    item=replace(deposit,wallet=wallet or created.user)
    slot,stamp,sig=(900,item.time-40*86400,'launch')
    if position=='current':slot,stamp,sig=item.slot,item.time,item.signature
    elif position=='later':slot,stamp=item.slot+1,item.time+1
    raw.update(slot=slot,blockTime=stamp)
    raw['transaction']['signatures']=[sig]
    if item.wallet not in [k['pubkey'] for k in raw['transaction']['message']['accountKeys']]:
        raw['transaction']['message']['accountKeys'].append({'pubkey':item.wallet,'signer':False})
        raw['meta']['preBalances'].append(0);raw['meta']['postBalances'].append(0)
    entries=[row(sig,slot,stamp)]
    if position!='current':entries.append(row(item.signature,item.slot,item.time))
    entries += [row('receipt'+str(i),950-i,item.time-35*86400) for i in range(count-len(entries))]
    entries.sort(key=lambda r:r['slot'],reverse=True)
    return item,HistoryRPC(item,entries,{sig:raw}),created


@pytest.mark.asyncio
@pytest.mark.parametrize('sample_row',SAMPLES,ids=lambda r:r['mint'][:6])
async def test_real_sol_and_non_sol_launches_block_cex_and_privacy(config,store,deposit,sample_row):
    item,rpc,created=history(deposit,sample(sample_row),count=10)
    service=Service(config,store)
    def unexpected(*args,**kwargs):raise AssertionError('admission rejection must not notify')
    service.notices.emit=unexpected
    assert not await service.qualify(item,rpc)
    assert not store.hotlist(time.time())
    state=json.loads(store.rows('funding','SELECT detail FROM funding_activity_checks')[0]['detail'])
    assert state['reason']=='previous-token-launch'
    assert state['previous_launch']['mint']==created.mint
    assert state['previous_launch']['launchpad']==created.launchpad
    assert state['total_transactions']==10
    assert state['previous_launch']['time']<item.time-30*86400
    assert store.rows('audit',"SELECT reason FROM decisions WHERE stage='qualification'")==[{'reason':'previous-token-launch'}]
    prior=list(rpc.calls)
    assert await qualify_activity(item,rpc,store)==(False,'previous-token-launch')
    assert rpc.calls==prior


@pytest.mark.asyncio
@pytest.mark.parametrize('factory',[lambda:pump(v2=False,cpi=True),lambda:pump(cpi=True),
    lambda:launchlab(kind='legacy',cpi=True),lambda:launchlab(kind='v2',cpi=True),lambda:launchlab(cpi=True)])
async def test_inner_create_variants_are_rejected(store,deposit,factory):
    item,rpc,_=history(deposit,factory())
    assert await qualify_activity(item,rpc,store)==(False,'previous-token-launch')


@pytest.mark.asyncio
@pytest.mark.parametrize('position',['current','later'])
async def test_launch_in_funding_or_later_row_is_also_rejected(store,deposit,position):
    item,rpc,_=history(deposit,pump(),position=position)
    assert await qualify_activity(item,rpc,store)==(False,'previous-token-launch')


@pytest.mark.asyncio
async def test_over_cap_skips_even_create_detail_fetches(store,deposit):
    item,rpc,_=history(deposit,pump(),count=11)
    assert await qualify_activity(item,rpc,store)==(False,'prelaunch-activity-too-high')
    assert [c[0] for c in rpc.calls]==['getSignaturesForAddress']


@pytest.mark.asyncio
@pytest.mark.parametrize('failed',[True,False])
async def test_old_failed_create_does_not_count_as_launched(store,deposit,failed):
    item,rpc,_=history(deposit,pump())
    if failed:rpc.details['launch']['meta']['err']={'InstructionError':[0,'Custom']}
    assert (await qualify_activity(item,rpc,store))[0] is failed


@pytest.mark.asyncio
@pytest.mark.parametrize('identity',['unsigned-creator','signed-creator','unrelated'])
async def test_supplied_creator_requires_signature_and_other_wallet_not_poisoned(store,deposit,identity):
    raw=pump();created=inspect(Tx(raw)).creates[0]
    wallet=created.creator if identity!='unrelated' else address()
    item,rpc,_=history(deposit,raw,wallet=wallet)
    if identity=='signed-creator':
        next(k for k in rpc.details['launch']['transaction']['message']['accountKeys'] if k['pubkey']==wallet)['signer']=True
    allowed,reason=await qualify_activity(item,rpc,store)
    assert allowed is (identity!='signed-creator')
    if not allowed:assert reason=='previous-token-launch'


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['missing','meta','instruction','version'])
async def test_unavailable_create_details_stay_pending_and_resume(store,deposit,damage):
    item,rpc,_=history(deposit,pump())
    valid=deepcopy(rpc.details['launch'])
    if damage=='missing':rpc.details['launch']=None
    elif damage=='meta':rpc.details['launch']['meta']=None
    elif damage=='instruction':rpc.details['launch']['transaction']['message']['instructions'][0]['accounts']=[]
    else:rpc.details['launch']['version']=99
    with pytest.raises(HistoryPending):await qualify_activity(item,rpc,store)
    assert not store.hotlist(time.time())
    rpc.details['launch']=valid
    assert await qualify_activity(item,rpc,store)==(False,'previous-token-launch')
    assert sum(c[0]=='getSignaturesForAddress' for c in rpc.calls)==1
    assert sum(c==('transaction','fund') for c in rpc.calls)==1


@pytest.mark.asyncio
async def test_previous_policy_approval_is_rechecked_for_launches(store,deposit):
    item,rpc,_=history(deposit,pump())
    state={'policy':2,'max_transactions':10,'cutoff':item.time-30*86400,
           'funding_slot':item.slot,'complete':True,'allowed':True,'reason':'no-signed-activity-30d'}
    with store.db('funding') as db:
        db.execute('INSERT INTO funding_activity_checks VALUES (?,?,?,?)',
                   (item.event,time.time(),item.time+86400,json.dumps(state)))
    assert await qualify_activity(item,rpc,store)==(False,'previous-token-launch')
