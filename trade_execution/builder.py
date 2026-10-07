"""JSON IPC to the pinned unsigned SDK builder; private keys stay in Python."""
import asyncio
from chain_common.public_rpc import sdk_event
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import time
from weakref import WeakKeyDictionary


class BuildError(RuntimeError):
    def __init__(self, reason, retry_after=0):
        super().__init__(reason)
        self.retry_after=retry_after


class JupiterGate:
    """Shared by service workers, across their short-lived Node subprocesses."""
    def __init__(self):
        self.lock=asyncio.Lock()
        self.ready=0.0

    @asynccontextmanager
    async def slot(self):
        async with self.lock:
            # Never queue a stale trade through a long provider cooldown.
            delay=self.ready-time.monotonic()
            if delay>2:
                raise BuildError('jupiter-rate-limited',retry_after=delay)
            if delay>0:
                await asyncio.sleep(delay)
            cooldown=2.0
            try:
                yield
            except BuildError as exc:
                cooldown=max(cooldown,exc.retry_after)
                raise
            finally:
                # Completion-to-start spacing is conservative: actual API
                # requests are at least two seconds apart, even if RPC work
                # before the HTTP request takes a variable amount of time.
                self.ready=time.monotonic()+cooldown


_jupiter_gates=WeakKeyDictionary()


async def build(config, route, wallet):
    if route.kind in ('sol_to_stonk_curve','sol_to_pump_curve'):
        loop=asyncio.get_running_loop()
        gate=_jupiter_gates.setdefault(loop,JupiterGate())
        async with gate.slot():
            return await _build(config,route,wallet)
    return await _build(config,route,wallet)


async def build_sell(config,request,wallet,amount):
    from types import SimpleNamespace
    return await _build(config,SimpleNamespace(request=lambda:request),wallet,sell_amount=amount)


async def build_sweep(config,request,wallet,amount):
    from types import SimpleNamespace
    return await _build(config,SimpleNamespace(request=lambda:request),wallet,sell_amount=amount,side='sweep')


async def _build(config, route, wallet, *, sell_amount=None,side='sell'):
    payload=payload_for(config,route.request(),wallet)
    if sell_amount is not None:
        payload.update(side=side,amount=str(sell_amount),commitment='confirmed')
    process=await asyncio.create_subprocess_exec(
        'node', str(Path(__file__).parent/'sdk'/'build.cjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=config.root, env=child_environment())
    async def stderr_events():
        while line:=await process.stderr.readline():
            try:
                sdk_event(json.loads(line))
            except (ValueError,UnicodeError):
                pass  # SDK diagnostic text can contain endpoint credentials.
    async def collect():
        process.stdin.write(json.dumps(payload).encode())
        await process.stdin.drain()
        process.stdin.close()
        stdout,_,_=await asyncio.gather(process.stdout.read(),stderr_events(),process.wait())
        return stdout
    try:
        stdout=await asyncio.wait_for(collect(),timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if not stdout or len(stdout)>100000:
        raise BuildError('route-builder-failed')
    result=json.loads(stdout)
    result_or_error(result)
    if process.returncode:
        raise BuildError('route-builder-failed')
    return result


def child_environment():
    return {k:v for k,v in os.environ.items()
            if not k.startswith(('SOL_','TELEGRAM_','ALCHEMY_','JUPITER_')) and k!='NODE_OPTIONS'}


def payload_for(config, request, wallet):
    return {**request, 'rpc':config.rpc, 'ws':config.ws, 'wallet':wallet,
            'amount':str(config.buy_amount), 'slippagePercent':format(config.slippage_percent,'f'),
            'minLiquidity':str(config.min_liquidity),'commitment':config.hotlist_commitment,
            'risk':{'poolFeeBps':config.max_pool_fee_bps, 'totalFeeBps':config.max_total_fee_bps,
                    'impactBps':config.max_price_impact_bps}}


def result_or_error(result):
    if result.get('error'):
        # Never echo RPC/provider/SDK exception text, which can contain the URL.
        reason=result['error']
        delay=result.get('retryAfter',60) if reason=='jupiter-rate-limited' else 0
        if not isinstance(delay,(int,float)) or not math.isfinite(delay) or delay<0:
            delay=60
        raise BuildError(reason if isinstance(reason,str) and len(reason)<80 and all(c.islower() or c=='-' for c in reason)
                         else 'route-build-or-simulation-rejected',retry_after=delay)
