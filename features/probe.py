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


async def probe(args):
    config=load()
    async with aiohttp.ClientSession() as session:
        rpc=Rpc(session,config.rpc,Priority(),background=True,rps=2)
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
                result={'signature':sig,'version':raw.get('version'),'funding':[f.dict() for f in funding(tx,config)],
                        'creates':[item.dict() for item in creation.creates],'create_rejections':creation.rejected,
                        'trades':[],'route_status':'trade-decoding-disabled'}
                if args.raw:
                    result['raw']=raw
                print(json.dumps(result,separators=(',',':')),flush=True)
            except Exception as exc:
                print(json.dumps({'signature':sig,'error_type':type(exc).__name__,'status':'incomplete'}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tx')
    p.add_argument('--address',default=pump_fun.PROGRAM,type=lambda s:str(pubkey(s)))
    p.add_argument('--mint',type=lambda s:str(pubkey(s)),help='Verify pump.fun/stonk origin only, for any mint')
    p.add_argument('--samples',type=int,default=3,choices=range(1,11))
    p.add_argument('--raw',action='store_true',help='Print the successfully parsed public transaction')
    asyncio.run(probe(p.parse_args()))


if __name__=='__main__':
    main()
