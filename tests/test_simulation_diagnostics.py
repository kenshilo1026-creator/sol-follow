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
    async def builder(*args):
        if source=='sdk':result_or_error({'error':'simulation-rejected','simulationDetails':detail})
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
    assert rpc.calls==(['simulateTransaction'] if source=='python' else [])
