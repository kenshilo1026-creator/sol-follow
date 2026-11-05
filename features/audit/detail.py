"""Keep bounded decision evidence, prioritizing failures and the latest progress."""
import json

LIMIT = 8000
PRIORITY = ('order','execution_stage','signed','buy_signature','slot','chain_time','side',
            'signal_age_s','signal_max_age_s','simulation_details','sdk_exception','timing_ms',
            'builder_diagnostics','build_diagnostics','rpc_details','simulation_source',
            'submit_started_at','submit_ack_at','source_proof','builder_timing')
NESTED_PRIORITY = ('exception','category','code','phase','reason','custom_code','failed_program',
                   'anchor_code','anchor_number','transaction_error','instruction_error','instruction_index',
                   'elapsed_ms','timeout_s','worker_ready','worker_boot_ms','worker_age_ms','progress',
                   'source_slot','quote','local_create','cache_lifecycle','rpc','reads','method','failures',
                   'address','role','source','outcome','ms','accounts')


def dumps(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def bounded(value, count, width, depth=0):
    if isinstance(value,str):
        return value if len(value)<=width else value[:width]+'…'
    if isinstance(value,(dict,list)) and depth>=8:
        return {'detail_truncated':True}
    if isinstance(value,list):
        # Last attempts/progress matter most when diagnosing the failure.
        return [bounded(v,count,width,depth+1) for v in value[-count:]]
    if isinstance(value,dict):
        keys=[k for k in NESTED_PRIORITY if k in value]
        keys.extend(k for k in value if k not in keys)
        return {k:bounded(value[k],count,width,depth+1) for k in keys[:24]}
    return value


def fit(value, budget):
    if len(dumps(value))<=budget:return value
    for count,width in ((8,256),(4,160),(2,96),(1,64)):
        candidate=bounded(value,count,width)
        if len(dumps(candidate))<=budget:return candidate
    if isinstance(candidate,dict):
        out={}
        for k,v in candidate.items():
            small=fit(v,max(32,budget//4)) if isinstance(v,(dict,list)) else v
            if len(dumps({**out,k:small}))<=budget:out[k]=small
        return out
    return {'detail_truncated':True}


def encode(value):
    original=dumps(value)
    if len(original)<=LIMIT:return original
    result={'detail_truncated':True,'detail_original_chars':len(original)}
    if not isinstance(value,dict):return dumps(result)
    keys=[k for k in PRIORITY if k in value]
    keys.extend(k for k in value if k not in keys)
    reduced=[];omitted=0
    for key in keys:
        budget=min(2000 if key in ('builder_diagnostics','build_diagnostics') else 1200,
                   LIMIT-len(dumps(result))-len(dumps(key))-160)
        if budget<64:
            omitted+=1
            continue
        compact=fit(value[key],budget)
        if compact!=value[key]:reduced.append(key)
        result[key]=compact
    # Per-field flags make the lossy summaries explicit, including shortened arrays.
    if omitted:result['detail_omitted_fields_count']=omitted
    if reduced and len(dumps({**result,'detail_reduced_fields':reduced}))<=LIMIT:
        result['detail_reduced_fields']=reduced
    return dumps(result)
