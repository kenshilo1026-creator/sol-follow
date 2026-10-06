"""Keyless limits must survive the fresh Node process used for each build."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from trade_execution import builder


def route(kind='sol_to_stonk_curve'):
    return SimpleNamespace(kind=kind,request=lambda:{'route':kind})


def test_concurrent_builds_share_gate_and_pump_bypasses_it(monkeypatch):
    async def scenario():
        clock=[100.0]
        monkeypatch.setattr(builder,'time',SimpleNamespace(monotonic=lambda:clock[0]))
        gate=builder.JupiterGate()
        builder._jupiter_gates[asyncio.get_running_loop()]=gate
        calls=[]
        first_started=asyncio.Event()
        finish_first=asyncio.Event()

        async def fake_build(config,selected,wallet):
            calls.append((wallet,clock[0]))
            if wallet=='first':
                first_started.set()
                await finish_first.wait()
                clock[0]+=3
            return wallet

        monkeypatch.setattr(builder,'_build',fake_build)
        first=asyncio.create_task(builder.build(None,route(),'first'))
        await first_started.wait()
        second=asyncio.create_task(builder.build(None,route(),'second'))
        await asyncio.sleep(0)
        assert calls==[('first',100.0)]
        assert await builder.build(None,route('pump_native_curve'),'pump')=='pump'
        # Advance our clock when the gate sleeps, without changing asyncio's clock.
        real_sleep=asyncio.sleep
        async def advance(delay):
            clock[0]+=delay
            await real_sleep(0)
        monkeypatch.setattr(builder.asyncio,'sleep',advance)
        finish_first.set()
        assert await asyncio.gather(first,second)==['first','second']
        assert calls==[('first',100.0),('pump',100.0),('second',105.0)]
    asyncio.run(scenario())


def test_provider_cooldown_blocks_new_children_and_recovers(monkeypatch):
    async def scenario():
        clock=[100.0]
        monkeypatch.setattr(builder,'time',SimpleNamespace(monotonic=lambda:clock[0]))
        attempts=[]
        async def fake_build(*args):
            attempts.append(clock[0])
            if len(attempts)==1:
                raise builder.BuildError('jupiter-rate-limited',retry_after=30)
            return {'ok':True}
        monkeypatch.setattr(builder,'_build',fake_build)
        with pytest.raises(builder.BuildError,match='jupiter-rate-limited'):
            await builder.build(None,route(),'wallet')
        clock[0]=110
        with pytest.raises(builder.BuildError) as error:
            await builder.build(None,route(),'wallet')
        assert error.value.retry_after==20
        assert attempts==[100]
        clock[0]=130
        assert await builder.build(None,route(),'wallet')=={'ok':True}
        assert attempts==[100,130]
    asyncio.run(scenario())


@pytest.mark.parametrize('retry,expected',[(30,30),('invalid',60),(-1,60)])
def test_keyless_ipc_preserves_cooldown_and_excludes_credentials(config,monkeypatch,retry,expected):
    monkeypatch.setenv('JUPITER_UNUSED_TOKEN','private-test-value')
    captured={}
    class Process:
        returncode=1
        async def communicate(self,payload):
            captured['payload']=json.loads(payload)
            return json.dumps({'error':'jupiter-rate-limited','retryAfter':retry}).encode(),b''
    async def spawn(*args,**kwargs):
        captured['env']=kwargs['env']
        return Process()
    monkeypatch.setattr(builder.asyncio,'create_subprocess_exec',spawn)
    with pytest.raises(builder.BuildError,match='jupiter-rate-limited') as error:
        asyncio.run(builder.build(config,route(),'public-wallet'))
    assert error.value.retry_after==expected
    assert captured['payload']=={'route':'sol_to_stonk_curve','rpc':config.rpc,'wallet':'public-wallet',
        'amount':str(config.buy_amount),'slippagePercent':format(config.slippage_percent,'f'),
        'minLiquidity':str(config.min_liquidity),
        'risk':{'poolFeeBps':200,'totalFeeBps':300,'impactBps':200}}
    assert not any(name.startswith('JUPITER_') for name in captured['env'])
