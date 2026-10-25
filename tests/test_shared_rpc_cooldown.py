import asyncio
import json
from pathlib import Path
import subprocess
import time

import pytest
from chain_common.endpoint_cooldown import EndpointCooldowns
from chain_common.rpc import Priority, Rpc, RpcError
from tests.test_rpc_alerts import Reply, Session


@pytest.mark.asyncio
async def test_real_429_shared_across_fresh_python_lanes(tmp_path):
    directory=tmp_path/'cooldown';url='https://example/secret'
    first=Rpc(Session(Reply(429,{})),url,Priority(EndpointCooldowns(directory)))
    with pytest.raises(RpcError):await first.call('getSlot',[])
    session=Session(Reply(200,{'result':42}))
    second=Rpc(session,url,Priority(EndpointCooldowns(directory)))
    with pytest.raises(RpcError,match='endpoint-cooling'):await second.call('getSlot',[])
    assert session.calls==0
    assert 'secret' not in str(list(directory.rglob('*')))


@pytest.mark.asyncio
async def test_request_error_and_node_unhealthy_do_not_spread(tmp_path):
    shared=EndpointCooldowns(tmp_path/'cooldown');url='https://example'
    for code in (-32602,-32005,-32016):
        first=Rpc(Session(Reply(200,{'error':{'code':code}})),url,Priority(shared))
        with pytest.raises(RpcError):await first.call('getSlot',[])
        other=Rpc(Session(Reply(200,{'result':42})),url,Priority(shared))
        assert await other.call('getSlot',[])==42


def test_python_node_deadlines_survive_processes_and_shorter_penalty(tmp_path):
    directory=tmp_path/'cooldown';url='https://example/secret'
    shared=EndpointCooldowns(directory)
    shared[url]=time.monotonic()+90
    module=Path(__file__).resolve().parents[1]/'trade_execution/sdk/endpoint-cooldown.cjs'
    script="const c=require(process.argv[1]).sharedCooldown(process.argv[2]); c.set(process.argv[3],Date.now()+1000); console.log(c.get(process.argv[3])-Date.now());"
    args=['node','-e',script,str(module),str(directory),url]
    assert float(subprocess.check_output(args,text=True))>80000
    fresh=EndpointCooldowns(directory)
    assert fresh.get(url)-time.monotonic()>80
    shared.clear()
    assert fresh.get(url)==0
    subprocess.check_output(args,text=True)
    assert 0<fresh.get(url)-time.monotonic()<=1.1


@pytest.mark.asyncio
async def test_nonstandard_rate_limit_code_shares_cooldown(tmp_path):
    shared=EndpointCooldowns(tmp_path/'cooldown');url='https://example'
    first=Rpc(Session(Reply(200,{'error':{'code':-32005,'message':'Rate limit exceeded'}})),url,Priority(shared))
    with pytest.raises(RpcError):await first.call('getSlot',[])
    session=Session(Reply(200,{'result':42}))
    with pytest.raises(RpcError,match='endpoint-cooling'):
        await Rpc(session,url,Priority(shared)).call('getSlot',[])
    assert session.calls==0
