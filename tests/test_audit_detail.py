import json
from features.audit.detail import encode
from features.audit.writer import record,BufferedDecisions


def oversized():
    return {
        'order':'test-order','execution_stage':'build','signed':False,'buy_signature':None,
        'slot':465662617,'signal_age_s':0.864,'signal_max_age_s':2,
        'simulation_details':{'reason':'transaction-error','custom_code':6005,'anchor_code':'BondingCurveComplete'},
        'sdk_exception':{'category':'range-error','phase':'serialize-transaction'},
        'timing_ms':{'precheck_ms':0.5,'failed_ms':522.052},
        'builder_diagnostics':{'elapsed_ms':522.052,'timeout_s':1.658,'worker_ready':True,
            'progress':[{'stage':'getMultipleAccountsInfo','phase':'end','ms':i} for i in range(31)]+
                       [{'stage':'getAddressLookupTable','phase':'end','ms':510.642}]},
        'build_diagnostics':{'exception':{'category':'range-error','phase':'serialize-transaction'},
            'source_slot':465662617,'rpc':[{'provider':'primary','outcome':'unavailable','ms':i} for i in range(32)],
            'reads':[{'phase':'cache','accounts':[{'address':'A'*44,'reason':'absent','role':'other'}]*32}]*16},
        'rpc_details':{'method':'getMultipleAccounts','failures':[{'reason':'execution-context-behind','required_slot':465662617}]},
        'extra':'x'*20000,
    }


def test_oversized_audit_retains_error_timing_and_latest_progress():
    data=oversized();text=encode(data);out=json.loads(text)
    assert len(text)<=8000 and out['detail_truncated']
    for key in ('order','signed','buy_signature','slot','signal_age_s','signal_max_age_s','timing_ms','simulation_details','sdk_exception','rpc_details'):
        assert out[key]==data[key]
    assert out['build_diagnostics']['exception']==data['build_diagnostics']['exception']
    assert out['builder_diagnostics']['progress'][-1]['ms']==510.642
    assert out['build_diagnostics']['rpc'][-1]['ms']==31
    assert out['detail_original_chars']>8000
    assert 'build_diagnostics' in out['detail_reduced_fields']


def test_small_audit_is_lossless_and_limit_holds_with_many_large_fields():
    small={'unicode':'測試','bool':False,'missing':None,'items':[1,2,3]}
    assert json.loads(encode(small))==small
    for n in (1,10,100,1000):
        text=encode({**oversized(),**{f'extra{i}':{'data':['y'*1000]*20} for i in range(n)}})
        assert len(text)<=8000
        assert json.loads(text)['order']=='test-order'


def test_direct_and_buffered_audit_write_preserve_summary(store):
    for buffered in (False,True):
        if buffered:store.decision_writer=BufferedDecisions(store,None)
        record(store,'execution','failed','sdk-or-rpc-failed',event=str(buffered),detail=oversized())
        if buffered:store.decision_writer.flush()
    rows=store.rows('audit','SELECT detail FROM decisions ORDER BY id')
    assert len(rows)==2
    for row in rows:
        assert len(row['detail'])<=8000
        detail=json.loads(row['detail'])
        assert detail['build_diagnostics']['exception']['phase']=='serialize-transaction'
        assert detail['builder_diagnostics']['progress'][-1]['ms']==510.642
