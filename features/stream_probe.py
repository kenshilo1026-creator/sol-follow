"""Bounded read-only Alchemy subscription probe. No DB, wallet keys or send RPC."""
import argparse
import asyncio
import hashlib
import json
import grpc
from chain_common.primitives import b58encode, pubkey
from features.funding.grpc_feed import channel_for, stream_call, subscription
from share_common.config import load


async def probe(config, count=10000, seconds=15, wallets=()):
    if not config.alchemy_key:
        return {'status':'missing-key','setting':'ALCHEMY_API_KEY'}
    # Deterministic inactive public keys test filter capacity without subscribing
    # to all Pump transactions. Optional real wallets test event delivery too.
    addresses=set(wallets)
    for i in range(count-len(addresses)):
        addresses.add(b58encode(hashlib.sha256(f'sol-follow-probe:{i}'.encode()).digest()))
    report={'requested_addresses':len(addresses),'messages':0,'transactions':0,
            'block_metadata':0,'protobuf_bytes':0,'status':'no-data'}
    try:
        async with channel_for(config) as channel:
            call=stream_call(channel,config)
            try:
                async with asyncio.timeout(seconds):
                    await call.write(subscription(addresses))
                    while report['messages']<200:
                        update=await call.read()
                        if update is grpc.aio.EOF:
                            report['status']='stream-ended'
                            break
                        report['messages']+=1
                        report['protobuf_bytes']+=update.ByteSize()
                        kind=update.WhichOneof('update_oneof')
                        if kind=='ping':
                            from chain_common.yellowstone import geyser_pb2 as pb
                            await call.write(pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
                        elif kind=='transaction':
                            report['transactions']+=1
                        elif kind=='block_meta':
                            report['block_metadata']+=1
                            report['status']='subscription-received-metadata'
            except TimeoutError:
                pass
            finally:
                call.cancel()
    except grpc.RpcError as exc:
        report['status']='grpc-'+exc.code().name
    except Exception as exc:
        report['status']=type(exc).__name__
    report['note']='Filter acceptance/transport probe only; no proof of complete wallet coverage or trading readiness.'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=10000)
    parser.add_argument('--seconds',type=int,default=15)
    parser.add_argument('--wallet',action='append',default=[],type=lambda x:str(pubkey(x)))
    args=parser.parse_args()
    if not 1<=args.count<=50000 or not 1<=args.seconds<=60 or len(set(args.wallet))>args.count:
        parser.error('count: 1..50000; seconds: 1..60; wallets must fit count')
    result=asyncio.run(probe(load(),args.count,args.seconds,args.wallet))
    print(json.dumps(result))
    if result['status']!='subscription-received-metadata':
        raise SystemExit(1)


if __name__=='__main__':
    main()
