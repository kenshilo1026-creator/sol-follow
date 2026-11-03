"""Bounded, allowlisted public build evidence from the unsigned worker."""
import math
import re
from chain_common.primitives import pubkey
from chain_common.rpc_diagnostics import rpc_details


def sanitize(value):
    if not isinstance(value,dict):return {}
    def number(v):return type(v) in (int,float) and math.isfinite(v) and 0<=v<=2**53-1
    def key(v):
        if not isinstance(v,str) or not 32<=len(v)<=44:return False
        try:pubkey(v);return True
        except (ValueError,TypeError):return False
    def fields(obj,numbers=(),booleans=(),enums=None):
        if not isinstance(obj,dict):return {}
        out={k:obj[k] for k in numbers if number(obj.get(k))}
        out.update({k:obj[k] for k in booleans if type(obj.get(k)) is bool})
        out.update({k:obj[k] for k,allowed in (enums or {}).items() if isinstance(obj.get(k),str) and obj[k] in allowed})
        return out
    result=fields(value,('source_slot',))
    quote=value.get('quote')
    if isinstance(quote,dict):
        result['quote']={k:quote[k] for k in ('amount','quoted_out','min_out','mint_supply',
            'virtual_token_reserves','virtual_quote_reserves','real_token_reserves','real_quote_reserves')
            if isinstance(quote.get(k),str) and re.fullmatch(r'[0-9]{1,40}',quote[k])}
        slip=quote.get('slippage_percent')
        if isinstance(slip,str) and re.fullmatch(r'[0-9]{1,3}(\.[0-9]{1,8})?',slip):result['quote']['slippage_percent']=slip
    local=fields(value.get('local_create'),booleans=('eligible',),enums={'reason':{
        'disabled','source-snapshot-unavailable','source-snapshot-ineligible','source-create-and-trade-events'}})
    if local:result['local_create']=local
    reads=value.get('reads')
    if isinstance(reads,list):
        result['reads']=[]
        for read in reads[:16]:
            r=fields(read,('ms','response_slot'),enums={'scope':{'quote'},'phase':{'cache','shared','projection'},
                'outcome':{'cache-hit','cache-only-miss','pending','websocket','http','unavailable'}})
            if not r:continue
            r['accounts']=[]
            for a in (read.get('accounts') if isinstance(read.get('accounts'),list) else [])[:32]:
                if not isinstance(a,dict) or not key(a.get('address')):continue
                safe=fields(a,('min_slot','row_slot','age_ms','head','ttl_ms','pulse_age_ms','recovery_failures'),('pinned','recovery_pending','continuous','subscribed','healthy','shared','websocket_connected'),{
                    'last_loss':{'never-loaded','evicted','subscription-gap','baseline-failed','websocket-close','websocket-error','generation-invalidated'},
                    'role':{'mint','pool','pump-global','pump-fee-config','other'},
                    'source':{'source-events','shared-cache','cache-read','cache-hit','cache-miss'},
                    'reason':{'hit','forced-baseline','not-shared','absent','clock-ahead','null-account',
                        'not-continuous','subscription-not-ready','websocket-unhealthy','head-behind','slot-behind','expired'}})
                safe['address']=a['address'];r['accounts'].append(safe)
            result['reads'].append(r)
    rpc=value.get('rpc')
    if isinstance(rpc,list):
        result['rpc']=[fields(r,('attempt','ms'),('hedged',),{
            'method':{'getMultipleAccounts','getAccountInfo','getLatestBlockhash','getAddressLookupTable',
                'getEpochInfo','simulateTransaction','getGenesisHash','getProgramAccounts'},
            'provider':{'primary','fallback'},
            'outcome':{'response','unavailable','rpc-rejection','simulation-rejected','cancelled'}}) for r in rpc[:32] if isinstance(r,dict)]
    if isinstance(rpc,list):
        for safe,raw in zip(result['rpc'],(r for r in rpc[:32] if isinstance(r,dict))):
            details=rpc_details({'method':'getMultipleAccounts','failures':[raw]})
            if details:safe.update(details['failures'][0])
    return result
