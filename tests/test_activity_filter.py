import time
import pytest
from features.funding.activity_filter import qualify_activity, HistoryPending, DAYS
from features.funding.decoder import Funding
from tests.helpers import address


@pytest.fixture(params=['cex:test','privacy-cash'])
def deposit(request):return Funding('fund-event','fund',address(),address(),request.param,10**9,1000,int(time.time()))


def row(sig,slot,stamp):return {'signature':sig,'slot':slot,'blockTime':stamp,'err':None}


class RPC:
    def __init__(self,item,pages,signers=()):self.item=item;self.pages=pages;self.signers=signers;self.calls=[];self.missing=False
    async def call(self,method,params):
        self.calls.append((method,params))
        assert method=='getSignaturesForAddress'
        assert params[1]['minContextSlot']==self.item.slot
        return self.pages[params[1].get('before')]
    async def transaction(self,sig):
        self.calls.append(('transaction',sig))
        if self.missing:return None
        entry=next(r for page in self.pages.values() for r in page if r['signature']==sig)
        return {'slot':entry['slot'],'meta':{'err':'failed'},'transaction':{'signatures':[sig],
            'message':{'accountKeys':[{'pubkey':self.item.wallet,'signer':sig in self.signers}]}}}


@pytest.mark.asyncio
async def test_new_wallet_and_current_deposit_excluded(store,deposit):
    rpc=RPC(deposit,{None:[row('fund',1000,deposit.time)]})
    assert await qualify_activity(deposit,rpc,store)==(True,'no-prior-signatures')
    assert len(rpc.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('days,signer,allowed',[(29,False,True),(29,True,False),(30,True,False),(31,True,True)])
async def test_only_signed_activity_and_strict_30_day_cutoff(store,deposit,days,signer,allowed):
    rpc=RPC(deposit,{None:[row('fund',1000,deposit.time),row('old',900,deposit.time-days*86400)]},['old'] if signer else [])
    assert (await qualify_activity(deposit,rpc,store))[0] is allowed
    # Failed signed transactions count too; receiving alone is not activity.


@pytest.mark.asyncio
async def test_post_deposit_activity_ignored(store,deposit):
    rpc=RPC(deposit,{None:[row('later',1001,deposit.time+1),row('fund',1000,deposit.time)]},['later'])
    assert (await qualify_activity(deposit,rpc,store))[0]
    assert not any(c[0]=='transaction' for c in rpc.calls)


@pytest.mark.asyncio
async def test_empty_or_unindexed_funding_retries_from_head(store,deposit):
    rpc=RPC(deposit,{None:[]})
    with pytest.raises(HistoryPending,match='funding-not'):await qualify_activity(deposit,rpc,store)
    assert store.rows('funding','SELECT * FROM funding_activity_checks')==[]
    rpc.pages[None]=[row('fund',1000,deposit.time)]
    assert (await qualify_activity(deposit,rpc,store))[0]


@pytest.mark.asyncio
async def test_missing_transaction_does_not_admit_and_resumes(store,deposit):
    rpc=RPC(deposit,{None:[row('fund',1000,deposit.time),row('old',900,deposit.time-1)]},['old']);rpc.missing=True
    with pytest.raises(HistoryPending):await qualify_activity(deposit,rpc,store)
    rpc.missing=False
    assert (await qualify_activity(deposit,rpc,store))[0] is False
    assert sum(c[0]=='getSignaturesForAddress' for c in rpc.calls)==1


@pytest.mark.asyncio
async def test_pagination_budget_resumes_without_repeating_checked_receipts(store,deposit,monkeypatch):
    import features.funding.activity_filter as module
    monkeypatch.setattr(module,'PAGE',2);monkeypatch.setattr(module,'BUDGET',2)
    rpc=RPC(deposit,{None:[row('fund',1000,deposit.time),row('receipt',950,deposit.time-10)],
        'receipt':[row('signed',900,deposit.time-20)]},['signed'])
    with pytest.raises(HistoryPending,match='incomplete'):await qualify_activity(deposit,rpc,store)
    assert (await qualify_activity(deposit,rpc,store))[0] is False
    assert sum(c==('transaction','receipt') for c in rpc.calls)==1


@pytest.mark.asyncio
async def test_same_slot_signed_order_unknown_is_pending(store,deposit):
    rpc=RPC(deposit,{None:[row('fund',1000,deposit.time),row('signed',1000,deposit.time)]},['signed'])
    with pytest.raises(HistoryPending,match='same-slot'):await qualify_activity(deposit,rpc,store)

@pytest.mark.asyncio
@pytest.mark.parametrize('missing',[False,True])
async def test_service_does_not_admit_active_or_unknown_wallet(config,store,deposit,missing):
    from features.runtime.service import Service
    from chain_common.primitives import SYSTEM
    class Public(RPC):
        async def call(self,method,params):
            if method=='getAccountInfo':return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
            return await super().call(method,params)
    rpc=Public(deposit,{None:[row('fund',1000,deposit.time),row('signed',900,deposit.time-100)]},['signed'])
    rpc.missing=missing
    if missing:
        with pytest.raises(HistoryPending):await Service(config,store).qualify(deposit,rpc)
    else:
        assert not await Service(config,store).qualify(deposit,rpc)
    assert store.hotlist(time.time())=={}
    assert store.rows('funding','SELECT * FROM funding')==[]


@pytest.mark.asyncio
async def test_missing_block_time_retries_then_resolves(store,deposit):
    class Times(RPC):
        stamp=None
        async def call(self,method,params):
            if method=='getBlockTime':
                assert params==[900]
                return self.stamp
            return await super().call(method,params)
    rpc=Times(deposit,{None:[row('fund',1000,deposit.time),row('signed',900,None)]},['signed'])
    with pytest.raises(HistoryPending,match='history-time-unavailable'):
        await qualify_activity(deposit,rpc,store)
    rpc.stamp=deposit.time-1
    assert (await qualify_activity(deposit,rpc,store))[0] is False


@pytest.mark.asyncio
async def test_history_cannot_skip_unindexed_deposit(store,deposit):
    rpc=RPC(deposit,{None:[row('old',900,deposit.time-31*86400)]})
    with pytest.raises(HistoryPending,match='funding-not-in-address-history'):
        await qualify_activity(deposit,rpc,store)


@pytest.mark.asyncio
@pytest.mark.parametrize('history',['new','incoming','dormant'])
async def test_service_admits_only_after_history_passes(config,store,deposit,history):
    from features.runtime.service import Service
    from chain_common.primitives import SYSTEM
    class Public(RPC):
        async def call(self,method,params):
            if method=='getAccountInfo':return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
            return await super().call(method,params)
    entries=[row('fund',1000,deposit.time)]
    if history!='new':entries.append(row('old',900,deposit.time-(31*86400 if history=='dormant' else 10)))
    rpc=Public(deposit,{None:entries},['old'] if history=='dormant' else [])
    assert await Service(config,store).qualify(deposit,rpc)
    assert deposit.wallet in store.hotlist(time.time())
    assert any(method=='getSignaturesForAddress' for method,_ in rpc.calls)
    assert store.rows('funding','SELECT * FROM funding_activity_checks')


@pytest.mark.asyncio
async def test_filter_rejection_does_not_emit_telegram(config,store,deposit):
    from features.runtime.service import Service
    from chain_common.primitives import SYSTEM
    class Public(RPC):
        async def call(self,method,params):
            if method=='getAccountInfo':return {'value':{'owner':SYSTEM,'executable':False,'data':['','base64']}}
            return await super().call(method,params)
    service=Service(config,store)
    def unexpected(*args,**kwargs):raise AssertionError('filter must not add notifications')
    service.notices.emit=unexpected
    rpc=Public(deposit,{None:[row('fund',1000,deposit.time),row('signed',900,deposit.time-1)]},['signed'])
    assert not await service.qualify(deposit,rpc)
