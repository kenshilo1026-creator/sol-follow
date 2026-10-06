"""Transaction priority spans both SDK builds and Python execution."""
from dataclasses import replace
import pytest
from chain_common.background_rpc import BackgroundRpc
from chain_common.rpc import Priority
from tests.helpers import Notices
from tests.test_pump_buy import setup_order, RPC
from trade_execution.executor import Executor


@pytest.mark.asyncio
async def test_background_rechecks_trade_after_rate_budget_wait(monkeypatch):
    import chain_common.background_rpc as module
    priority=Priority()
    now=[0.0]
    sleeps=[]
    class Public:
        async def call(self, method, params):
            assert priority.active==0
            return 'read'
    rpc=BackgroundRpc(Public(),priority,2)
    rpc.next_request=1.0
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps)==1:
            now[0]=1.0
            priority.active=1
        else:
            assert priority.active==1
            priority.active=0
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    assert await rpc.call('getBlockHeight',[])=='read'
    assert sleeps==[1.0,0.05]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[False,True])
async def test_node_priority_covers_python_simulation_and_releases_on_failure(config,store,failure):
    cfg,route,oid,build=setup_order(config,store)
    priority=Priority()
    states=[]
    class Builder:
        async def set_priority(self, active):
            assert active==priority.active
            states.append(active)
        async def __call__(self,*args):
            assert states==[1] and priority.active==1
            return await build(*args)
    class Public(RPC):
        async def call(self,*args):
            assert states==[1] and priority.active==1
            return await super().call(*args)
    await Executor(cfg,store,Public(simulation_error=failure),Notices(),priority,Builder()).buy(oid,route)
    assert states==[1,0] and priority.active==0
    assert store.order(oid)['state']==('failed' if failure else 'dry-simulated')


@pytest.mark.asyncio
async def test_order_reconciliation_uses_background_rpc(config,store):
    cfg,route,oid,build=setup_order(replace(config,dry_run=False),store)
    store.update_order(oid,state='submitted',signature='mock-signature',last_height=100,reason='atomic-pump-buy-v1')
    direct=RPC()
    background=RPC()
    await Executor(cfg,store,direct,Notices(),Priority(),build,reconcile_rpc=background).reconcile_once()
    assert direct.calls==[]
    assert background.calls==['getSignatureStatuses','getBlockHeight']
