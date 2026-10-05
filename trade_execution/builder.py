"""JSON IPC to the pinned unsigned SDK builder; private keys stay in Python."""
import asyncio
import json
import os
from pathlib import Path


class BuildError(RuntimeError):
    pass


async def build(config, route, wallet):
    payload={**route.request(), 'rpc':config.rpc, 'wallet':wallet,
             'amount':str(config.buy_amount), 'slippagePercent':format(config.slippage_percent,'f'),
             'minLiquidity':str(config.min_liquidity)}
    process=await asyncio.create_subprocess_exec(
        'node', str(Path(__file__).parent/'sdk'/'build.cjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=config.root, env={k:v for k,v in os.environ.items()
                             if not k.startswith(('SOL_','TELEGRAM_','ALCHEMY_')) and k!='NODE_OPTIONS'})
    try:
        stdout,_=await asyncio.wait_for(process.communicate(json.dumps(payload).encode()),timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if not stdout or len(stdout)>100000:
        raise BuildError('route-builder-failed')
    result=json.loads(stdout)
    if result.get('error'):
        # Never echo RPC/provider/SDK exception text, which can contain the URL.
        reason=result['error']
        raise BuildError(reason if isinstance(reason,str) and len(reason)<80 and all(c.islower() or c=='-' for c in reason)
                         else 'route-build-or-simulation-rejected')
    if process.returncode:
        raise BuildError('route-builder-failed')
    return result
