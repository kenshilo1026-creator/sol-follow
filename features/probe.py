"""Bounded, read-only mainnet compatibility probe. Never opens DB or wallet keys."""
import argparse
import asyncio
import json
import aiohttp
from chain_common.rpc import Rpc,Priority
from chain_common.transaction import Tx
from chain_common.primitives import pubkey
from features.funding.decoder import decode as funding
from share_common.config import load
from launchpads import LaunchpadScope, pump_fun
from launchpads.create import inspect as inspect_creates
from trade_execution.route import decode as decode_buy_route
from trade_execution.builder import build, BuildError


async def probe(args):
    config=load()
    async with aiohttp.ClientSession() as session:
        rpc=Rpc(session,config.rpc,Priority())
        scope=LaunchpadScope()
        if args.mint:
            origin=await scope.classify(rpc,args.mint)
            print(json.dumps({'mint':args.mint,'origin':origin.__dict__ if origin else None,
                              'note':'origin proof only; does not certify an executable trade route'}))
            return
        if args.tx:
            signatures=[args.tx]
        else:
            rows=await rpc.call('getSignaturesForAddress',[args.address,{'limit':args.samples,'commitment':'finalized'}])
            signatures=[r['signature'] for r in rows if r['err'] is None]
        for sig in signatures:
            try:
                raw=await rpc.transaction(sig,'finalized')
                if raw is None:
                    print(json.dumps({'signature':sig,'status':'not-indexed'}),flush=True)
                    continue
                tx=Tx(raw)
                creation=inspect_creates(tx)
                route=decode_buy_route(tx)
                result={'signature':sig,'version':raw.get('version'),'funding':[f.dict() for f in funding(tx,config)],
                        'creates':[item.dict() for item in creation.creates],'create_rejections':creation.rejected,
                        'trades':[route.trade.__dict__] if route else [],
                        'route_status':'meteora_dlmm_to_pump_curve' if route else 'unsupported'}
                if args.buy_route and route:
                    built=await build(config,route,config.wallet_address or route.trade.wallet)
                    result['buy_simulation']={k:v for k,v in built.items() if k!='transaction'}
                if args.raw:
                    result['raw']=raw
                print(json.dumps(result,separators=(',',':')),flush=True)
            except Exception as exc:
                print(json.dumps({'signature':sig,'error_type':type(exc).__name__,'status':'incomplete',
                                  **({'reason':str(exc)} if isinstance(exc,BuildError) else {})}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tx')
    p.add_argument('--address',default=pump_fun.PROGRAM,type=lambda s:str(pubkey(s)))
    p.add_argument('--mint',type=lambda s:str(pubkey(s)),help='Verify pump.fun/stonk origin only, for any mint')
    p.add_argument('--samples',type=int,default=3,choices=range(1,11))
    p.add_argument('--raw',action='store_true',help='Print the successfully parsed public transaction')
    p.add_argument('--buy-route',action='store_true',help='Fresh quote and unsigned atomic buy simulation; requires --tx, never signs or sends')
    args=p.parse_args()
    if args.buy_route and not args.tx:
        p.error('--buy-route requires --tx')
    asyncio.run(probe(args))


if __name__=='__main__':
    main()
