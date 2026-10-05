import base64
from solders.keypair import Keypair
from chain_common.primitives import discriminator
from launchpads import pump_fun


def pump_origin(slot=200):
    raw=discriminator('account','BondingCurve')+bytes(41)
    return {'context':{'slot':slot},'value':{'owner':pump_fun.PROGRAM,'executable':False,
            'data':[base64.b64encode(raw).decode(),'base64']}}


def address():
    return str(Keypair().pubkey())


def transaction(keys,signers,instructions,pre=None,post=None,inner=None,pre_tokens=None,post_tokens=None,slot=100,when=1000,sig='sample'):
    return {'slot':slot,'blockTime':when,'version':0,'transaction':{'signatures':[sig],'message':{
        'accountKeys':[{'pubkey':k,'signer':k in signers,'writable':True,'source':'transaction' if i<2 else 'lookupTable'}
                       for i,k in enumerate(keys)],'instructions':instructions}},'meta':{
        'err':None,'fee':5000,'preBalances':pre or [10**9]*len(keys),'postBalances':post or [10**9]*len(keys),
        'innerInstructions':inner or [],'preTokenBalances':pre_tokens or [],'postTokenBalances':post_tokens or []}}


class Notices:
    def __init__(self):self.items=[]
    def emit(self,*args,**kwargs):self.items.append((args,kwargs))
