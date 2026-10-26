"""First observations per feed, independent of mutable execution proof rows."""
from features.audit.writer import record


def observed(store,signature,source,phase,slot,received_at,**detail):
    # Existing audit retention applies. Unique event identity preserves first
    # observation when streams replay; no provider bodies or credentials.
    record(store,'feed','observed',source+'-'+phase,signature=signature,
           event=signature+':feed:'+source+':'+phase,
           detail={'source':source,'phase':phase,'slot':slot,
                   'received_at':received_at,**detail})


def compare(events):
    grouped={}
    for event in events:
        d=event['detail']
        if d.get('phase')!='received':continue
        stamp=d.get('received_at')
        if not isinstance(stamp,(int,float)):continue
        sources=grouped.setdefault(event['signature'],{})
        source=d.get('source')
        if source in ('grpc-processed','create-confirmed'):
            sources[source]=min(stamp,sources.get(source,stamp))
    result=[]
    for signature,sources in grouped.items():
        grpc=sources.get('grpc-processed');confirmed=sources.get('create-confirmed')
        delta=(confirmed-grpc)*1000 if grpc is not None and confirmed is not None else None
        result.append({'signature':signature,'grpc_received_at':grpc,'confirmed_log_received_at':confirmed,
                       'confirmed_minus_grpc_ms':delta,
                       'first_observed':('grpc-processed' if delta>0 else 'create-confirmed' if delta<0 else 'same-time')
                       if delta is not None else 'insufficient-evidence'})
    return result
