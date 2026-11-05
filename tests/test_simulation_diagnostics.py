import json
import pytest
from chain_common.simulation_diagnostics import from_response,sanitize
from trade_execution.builder import result_or_error,BuildError
from tests.helpers import address,Notices


def test_custom_error_survives_sdk_ipc_without_raw_logs_or_endpoint():
    program=address()
    response={'context':{'slot':123},'value':{'err':{'InstructionError':[2,{'Custom':6002}]},
        'unitsConsumed':75000,'logs':[
            'Program log: https://rpc.invalid/secret',
            'Program log: AnchorError caused by account: curve. Error Code: TooMuchSolRequired. Error Number: 6002. Error Message: hidden.',
            f'Program {program} failed: custom program error: 0x1772']}}
    detail=from_response(response)
    assert detail=={'reason':'transaction-error','slot':123,'units_consumed':75000,
                   'instruction_index':2,'custom_code':6002,'failed_program':program,
                   'anchor_code':'TooMuchSolRequired','anchor_number':6002}
    with pytest.raises(BuildError) as caught:
        result_or_error({'error':'simulation-rejected','simulationDetails':{**detail,'logs':response['value']['logs'],'url':'secret'}})
    assert caught.value.simulation_details==detail
    assert 'secret' not in json.dumps(caught.value.simulation_details)


@pytest.mark.parametrize('value,expected',[
    ({'err':'BlockhashNotFound'}, {'reason':'transaction-error','transaction_error':'BlockhashNotFound'}),
    ({'err':{'InstructionError':[1,'ComputationalBudgetExceeded']}},
     {'reason':'transaction-error','instruction_index':1,'instruction_error':'ComputationalBudgetExceeded'}),
    ({'err':{'InsufficientFundsForRent':{'account_index':3}}},
     {'reason':'transaction-error','transaction_error':'InsufficientFundsForRent','account_index':3}),
    ({'err':None},{'reason':'missing-compute-units'}),
    ({'err':None,'unitsConsumed':0},{'reason':'invalid-compute-units','units_consumed':0}),
])
def test_error_categories(value,expected):
    assert from_response({'value':value})==expected


def test_untrusted_error_fields_cannot_leak_provider_text():
    assert sanitize({'reason':'https://secret','custom_code':True,'slot':-1,'units_consumed':float('nan'),
        'failed_program':'https://secret','transaction_error':'secret','instruction_error':{},
        'anchor_code':'https://secret','logs':['secret']})=={}


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['sdk','python'])
async def test_simulation_failure_keeps_audit_evidence_and_never_submits(config,store,source):
    from tests.test_pump_buy import setup_order,RPC
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    cfg,route,oid,build=setup_order(config,store)
    result=await build()
    response={'context':{'slot':route.trade.slot},'value':{'err':{'InstructionError':[2,{'Custom':6002}]},'unitsConsumed':60000}}
    detail=from_response(response)
    evidence={'quote':{'amount':'100000000','min_out':'1234','quoted_out':'1500'},'source_slot':route.trade.slot}
    result['buildDiagnostics']=evidence
    async def builder(*args):
        if source=='sdk':result_or_error({'error':'simulation-rejected','simulationDetails':detail,'buildDiagnostics':evidence})
        return result
    class Reader(RPC):
        async def call(self,method,args):
            self.calls.append(method)
            assert method=='simulateTransaction'
            return response
    rpc=Reader()
    await Executor(cfg,store,rpc,Notices(),Priority(),builder).buy(oid,route)
    row=store.order(oid)
    assert row['state']=='failed' and row['reason']=='simulation-rejected'
    assert row['signature'] is None and row['raw'] is None
    audit=json.loads(store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]['detail'])
    assert audit['simulation_details']==detail
    assert audit['build_diagnostics']==evidence
    assert rpc.calls==(['simulateTransaction'] if source=='python' else [])


def test_build_evidence_is_bounded_and_drops_raw_secrets():
    from chain_common.build_diagnostics import sanitize
    account=address()
    value={'source_slot':100,'quote':{'amount':'100','min_out':'80','quoted_out':'100',
        'slippage_percent':'20','rpc':'https://secret','mint_supply':'bad'},
        'reads':[{'phase':'cache','outcome':'http','ms':212.4,'url':'secret','accounts':[
            {'address':account,'role':'pump-global','source':'cache-miss','reason':'not-continuous',
             'row_slot':99,'subscribed':True,'data':'secret'}]*40}]*20,
        'rpc':[{'method':'getMultipleAccounts','provider':'primary','attempt':1,'ms':212.4,
            'outcome':'unavailable','reason':'execution-context-behind','rpc_code':-32016,
            'required_slot':100,'context_slot':99,'url':'secret'}]*40}
    safe=sanitize(value)
    assert len(safe['reads'])==16 and len(safe['reads'][0]['accounts'])==32 and len(safe['rpc'])==32
    assert safe['reads'][0]['accounts'][0]['reason']=='not-continuous'
    assert safe['quote']=={'amount':'100','min_out':'80','quoted_out':'100','slippage_percent':'20'}
    assert safe['rpc'][0]['rpc_code']==-32016 and safe['rpc'][0]['context_slot']==99
    assert 'secret' not in json.dumps(safe)
    assert sanitize({'reads':[None,{'phase':'cache','accounts':[None,{'address':'secret'}]}],
        'rpc':[None],'source_slot':True})=={'reads':[{'phase':'cache','accounts':[]}],'rpc':[]}


def test_completed_curve_has_one_specific_copyable_notification():
    from share_common.notify import telegram_message
    from chain_common.simulation_diagnostics import pump_curve_complete
    detail={'mode':'live','mint':address(),'signed':False,'outcome':'not-submitted',
            'reason':'simulation-rejected','simulation_details':{'reason':'transaction-error',
            'failed_program':'6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
            'custom_code':6005,'anchor_code':'BondingCurveComplete','slot':123}}
    text=telegram_message('buy rejected',detail)
    assert '曲線已完成，跳過買入' in text and '6005 / BondingCurveComplete' in text
    assert '<code>'+detail['mint']+'</code>' in text
    assert '跟買失敗' not in text and '同 slot' not in text
    for change in [{'failed_program':address()},{'custom_code':6042},{'anchor_code':'OtherError'}, {'anchor_number':6002}]:
        assert not pump_curve_complete({**detail['simulation_details'],**change})
    assert '曲線已完成' not in telegram_message('buy deferred',{**detail,'signed':True,'outcome':'unknown'})
    assert '曲線已完成' not in telegram_message('buy rejected',{**detail,'simulation_details':{}})


def test_cache_recovery_diagnostics_are_safe_and_retained():
    from chain_common.build_diagnostics import sanitize
    value={'reads':[{'phase':'shared','accounts':[{'address':address(),'last_loss':'baseline-failed',
        'pinned':True,'recovery_pending':True,'recovery_failures':2}]}]}
    account=sanitize(value)['reads'][0]['accounts'][0]
    assert account['last_loss']=='baseline-failed' and account['pinned'] and account['recovery_failures']==2
    value['reads'][0]['accounts'][0]['last_loss']='https://secret'
    assert 'secret' not in json.dumps(sanitize(value))


@pytest.mark.asyncio
async def test_completed_curve_executor_emits_only_one_alert_without_submit(config,store):
    from tests.test_pump_buy import setup_order,RPC
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    from share_common.notify import telegram_message
    cfg,route,oid,_=setup_order(config,store)
    detail={'reason':'transaction-error','failed_program':'6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
            'custom_code':6005,'anchor_code':'BondingCurveComplete','slot':route.trade.slot}
    async def builder(*args):raise BuildError('simulation-rejected',simulation_details=detail)
    notices=Notices();rpc=RPC()
    await Executor(cfg,store,rpc,notices,Priority(),builder).buy(oid,route)
    alerts=[args for args,kwargs in notices.items if kwargs.get('alert')]
    assert len(alerts)==1 and '曲線已完成，跳過買入' in telegram_message(*alerts[0])
    assert store.order(oid)['signature'] is None and not rpc.calls


def test_cache_lifecycle_survives_failure_ipc_and_drops_untrusted_values():
    from chain_common.build_diagnostics import sanitize
    evidence={'cache_lifecycle':{'generation_start':2,'generation_end':3,
        'execution_generation_start':1,'execution_generation_end':1,
        'last_invalidation':'websocket-close'},
        'reads':[{'phase':'cache','outcome':'http','continued_after_disconnect':True,'accounts':[]}]}
    with pytest.raises(BuildError) as caught:
        result_or_error({'error':'cache-disconnected','buildDiagnostics':evidence})
    assert caught.value.build_diagnostics==evidence
    assert sanitize({'cache_lifecycle':{'generation_start':True,'generation_end':-1,
        'execution_generation_start':float('nan'),'last_invalidation':'https://secret'},
        'reads':[{'phase':'cache','continued_after_disconnect':'secret'}]})=={
            'reads':[{'phase':'cache','accounts':[]}]}


def test_last_known_good_setting_evidence_survives_ipc():
    from chain_common.build_diagnostics import sanitize
    account={'address':address(),'role':'pump-global','source':'cache-hit',
             'reason':'last-known-good','last_known_good':True,'age_ms':60000,'row_slot':100}
    value={'reads':[{'phase':'shared','accounts':[account]}]}
    assert sanitize(value)==value


def test_simulation_timeout_reports_unfinished_stage_without_claiming_curve_complete():
    from share_common.notify import telegram_message
    detail = {'stage': 'build', 'reason': 'signal-expired', 'signed': False,
              'outcome': 'not-submitted', 'builder_diagnostics': {
                  'phase': 'worker-response', 'elapsed_ms': 1048.276, 'timeout_s': 1.0473806,
                  'progress': [
                      {'stage': 'getLatestBlockhash', 'phase': 'start', 'ms': 9.96},
                      {'stage': 'getLatestBlockhash', 'phase': 'end', 'ms': 10.343},
                      {'stage': 'simulateTransaction', 'phase': 'start', 'ms': 21.493}]}}
    text = telegram_message('buy rejected', detail)
    assert '原因: signal-expired' in text
    assert '逾時位置: 模擬（simulateTransaction）已開始，尚未記錄完成' in text
    assert '本次組單等待上限: 1047.4 ms' in text
    assert '本次組單等待耗時: 1048.3 ms' in text
    assert '曲線已完成，跳過買入' not in text and '6005' not in text
    detail['builder_diagnostics']['progress'].append(
        {'stage': 'simulateTransaction', 'phase': 'end', 'ms': 25})
    text = telegram_message('buy rejected', detail)
    assert '逾時位置: 等待組單 worker 回覆' in text
    assert '模擬結果: 未取得' not in text


def test_timeout_diagnostics_ignore_malformed_and_untrusted_progress():
    from features.notifications.buy_failures import build_timeout_lines
    detail = {'stage': 'build', 'reason': 'signal-expired', 'builder_diagnostics': {
        'phase': [], 'elapsed_ms': float('nan'), 'timeout_s': True,
        'progress': [None, {'stage': [], 'phase': 'start'},
                     {'stage': 'https://secret', 'phase': 'start'}]}}
    assert build_timeout_lines(detail) == []
    detail['builder_diagnostics'] = {'phase': 'worker-response', 'progress': None}
    assert build_timeout_lines(detail) == ['逾時位置: 等待組單 worker 回覆']
    detail['reason'] = 'simulation-rejected'
    assert build_timeout_lines(detail) == []


@pytest.mark.asyncio
async def test_safe_sdk_exception_reaches_order_audit_and_notification(config,store):
    from tests.test_pump_buy import setup_order,RPC
    from trade_execution.executor import Executor
    from chain_common.rpc import Priority
    from share_common.notify import telegram_message
    cfg,route,oid,_=setup_order(config,store)
    evidence={'exception':{'category':'range-error','phase':'serialize-transaction',
              'code':'ERR_OUT_OF_RANGE','message':'PRIVATE_KEY','stack':'https://secret'},'source_slot':route.trade.slot}
    async def builder(*args):
        result_or_error({'error':'sdk-or-rpc-failed','buildDiagnostics':evidence})
    notices=Notices();rpc=RPC()
    await Executor(cfg,store,rpc,notices,Priority(),builder).buy(oid,route)
    row=store.order(oid)
    assert row['reason']=='sdk-or-rpc-failed' and row['signature'] is None
    audit=json.loads(store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")[0]['detail'])
    expected={'category':'range-error','phase':'serialize-transaction','code':'ERR_OUT_OF_RANGE'}
    assert audit['build_diagnostics']['exception']==expected
    alerts=[args for args,kwargs in notices.items if kwargs.get('alert')]
    text=telegram_message(*alerts[0])
    assert 'range-error / ERR_OUT_OF_RANGE / serialize-transaction' in text
    assert 'PRIVATE_KEY' not in text+json.dumps(audit) and 'https://secret' not in json.dumps(audit)
    assert not rpc.calls


def test_sdk_exception_enum_validation_drops_untrusted_payload():
    from chain_common.build_diagnostics import sanitize
    assert sanitize({'exception':{'category':[], 'phase':'https://secret', 'code':'secret', 'stack':'secret'}})=={}
