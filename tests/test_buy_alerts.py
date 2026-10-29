"""Failure alerts contain actionable public context without leaking RPC secrets."""
import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import pytest
from solders.keypair import Keypair
from chain_common.rpc import Priority, RpcError
from chain_common.transaction import Tx
from features.funding.decoder import Funding
from features.runtime.service import Service
from features.notifications.buy_failures import reason
from share_common.notify import Notices as TelegramNotices
from trade_execution.builder import BuildError
from trade_execution.executor import Executor
from trade_execution.route import decode_all
from tests.helpers import Notices, address
from tests.test_pump_buy import setup_order, RPC
from tests.test_pump_quote import direct


def alerts(notices, kind):
    return [args[1] for args,kw in notices.items if args[0]==kind and kw.get('alert')]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['cache-miss','unsupported'])
async def test_non_sol_create_build_failure_keeps_alert_and_consumption(config,store,reason):
    from tests.create_snipe_helpers import setup_create
    case=setup_create(config,store,non_sol=True)
    async def fail(*args):raise BuildError('price-cache-miss' if reason=='cache-miss' else 'pool-fee-limit')
    case.service.executor=Executor(case.service.config,store,case.rpc,case.service.notices,
                                   case.service.priority,builder=fail)
    await case.service.process(case.row,case.rpc)
    rows=alerts(case.service.notices,'buy rejected')
    assert len(rows)==1 and rows[0]['stage']=='build'
    assert rows[0]['mint']==case.item.mint and rows[0]['source_signature']==case.item.signature
    assert case.item.user not in store.hotlist(time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['sol_to_pump_curve','sol_to_stonk_curve'])
async def test_build_failures_alert_for_every_token(config,store,kind):
    cfg,route,oid,_=setup_order(config,store);route=replace(route,kind=kind)
    async def fail(*args):raise BuildError('pool-fee-limit')
    notices=Notices()
    await Executor(cfg,store,RPC(),notices,Priority(),fail).buy(oid,route)
    row=alerts(notices,'buy rejected')[0]
    assert row['reason']=='pool-fee-limit' and row['stage']=='build'
    assert row['mint']==route.trade.mint and row['quote_mint']==route.quote_mint
    assert row['source_signature']==route.trade.signature and row['route']==kind
    assert row['outcome']=='not-submitted' and row['mode']=='dry'


@pytest.mark.asyncio
async def test_simulation_failure_alert(config,store):
    cfg,route,oid,builder=setup_order(config,store);notices=Notices()
    await Executor(cfg,store,RPC(simulation_error=True),notices,Priority(),builder).buy(oid,route)
    row=alerts(notices,'buy rejected')[0]
    assert row['stage']=='simulate' and row['reason']=='simulation-rejected'


@pytest.mark.asyncio
async def test_ambiguous_send_keeps_reservation_and_alerts_unknown(config,store,tmp_path):
    key=Keypair()
    cfg,route,oid,builder=setup_order(replace(config,dry_run=False,wallet_keypair=key),store,key)
    notices=Notices()
    await Executor(cfg,store,RPC(store,oid,send_error=True),notices,Priority(),builder).buy(oid,route)
    row=alerts(notices,'buy deferred')[0]
    assert row['outcome']=='unknown' and row['stage']=='submit' and row['signature']
    assert store.order(oid)['state']=='unknown'


def test_error_details_never_forward_provider_url_or_key():
    for exc in [ValueError('https://rpc.example/private-key'),RuntimeError('private-key'),BuildError('https://rpc.example/private-key')]:
        assert reason(exc)==type(exc).__name__
    assert reason(RpcError('HTTP-429'))=='HTTP-429'
    assert reason(RpcError('sendTransaction:RPC--32002'))=='sendTransaction:RPC--32002'


@pytest.mark.asyncio
@pytest.mark.parametrize('code,actual,limit,chinese',[
    ('quote-token-tax-limit','12500','10000','報價幣轉帳稅率超過上限'),
    ('token-tax-limit','30000','20000','目標代幣轉帳稅率超過上限'),
])
async def test_fee_failure_details_reach_chinese_alert_and_audit(config,store,code,actual,limit,chinese):
    from trade_execution.builder import result_or_error
    from share_common.notify import message
    cfg,route,oid,_=setup_order(config,store)
    async def fail(*args):
        result_or_error({'error':code,'feeDetails':{'actualPpm':actual,'limitPpm':limit,
                                                   'secret':'https://private-key'}})
    notices=Notices()
    await Executor(cfg,store,RPC(),notices,Priority(),fail).buy(oid,route)
    detail=alerts(notices,'buy rejected')[0]
    rendered=message('buy rejected',detail)
    assert chinese in rendered and 'private-key' not in rendered
    assert detail['fee_actual_percent']=={'12500':'1.25%','100001':'10.0001%','30000':'3%'}[actual]
    assert detail['fee_limit_percent']=={'10000':'1%','100000':'10%','20000':'2%'}[limit]
    assert detail['fee_parameter']==('SOL_MAX_QUOTE_TOKEN_TAX_PERCENT' if code=='quote-token-tax-limit' else 'SOL_MAX_TOKEN_TAX_PERCENT')
    recorded=store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")
    assert json.loads(recorded[-1]['detail'])['fee_actual_percent']==detail['fee_actual_percent']


def test_fee_error_rejects_unsafe_details_and_handles_missing_values(config):
    from features.notifications.buy_failures import fee_context
    exc=BuildError('quote-token-tax-limit',fee_details={'actualPpm':'https://secret','limitPpm':'10000'})
    detail=fee_context(exc,config)
    assert detail['fee_actual_percent']=='未取得' and detail['fee_limit_percent']=='3.9%'
    assert not BuildError('unknown-error',fee_details={'actualPpm':'12500','limitPpm':'10000'}).fee_details


@pytest.mark.asyncio
async def test_rpc_failure_details_reach_order_audit_and_chinese_alert(config,store):
    from trade_execution.builder import result_or_error
    from share_common.notify import message
    cfg,route,oid,_=setup_order(config,store)
    async def fail(*args):
        result_or_error({'error':'execution-context-behind','rpcDetails':{
            'method':'getMultipleAccounts','secret':'https://private-key', 'failures':[
                {'provider':'primary','source':'alchemy','reason':'execution-context-behind',
                 'rpc_code':-32016,'required_slot':100,'context_slot':99,'attempt':7,'url':'https://private-key'},
                {'provider':'fallback','reason':'execution-endpoint-cooling'},
            ]}})
    notices=Notices()
    await Executor(cfg,store,RPC(),notices,Priority(),fail).buy(oid,route)
    detail=alerts(notices,'buy rejected')[0]
    text=message('buy rejected',detail)
    assert '主 RPC（Alchemy）: 節點尚未追上所需 slot' in text
    assert '備援 RPC: 端點冷卻中，未再請求' in text
    assert 'RPC 方法: getMultipleAccounts' in text
    assert '所需 slot 100，回報 slot 99' in text
    assert '嘗試 7 次' in text
    assert detail['signed'] is False and detail['outcome']=='not-submitted'
    recorded=store.rows('audit',"SELECT detail FROM decisions WHERE stage='execution'")
    assert json.loads(recorded[-1]['detail'])['rpc_details']==detail['rpc_details']
    assert 'private-key' not in json.dumps(detail)+recorded[-1]['detail']+text


def test_rpc_diagnostics_reject_untrusted_fields():
    from chain_common.rpc_diagnostics import rpc_details
    assert rpc_details({'method':'https://secret','failures':[]})=={}
    rows=[{'reason':[]},{'reason':'https://secret'},{'reason':'execution-timeout',
          'provider':'https://secret','http_status':True,'context_slot':'secret','rpc_code':-32016}]
    assert rpc_details({'method':'getMultipleAccounts','failures':rows})=={
        'method':'getMultipleAccounts','failures':[{'reason':'execution-timeout','rpc_code':-32016}]}


@pytest.mark.asyncio
async def test_alert_reaches_telegram_sender_without_real_network(config,store,monkeypatch):
    sent=[]
    notices=TelegramNotices(replace(config,telegram_token='fixture-token',telegram_chat='fixture-chat'),store)
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def post(self,url,**kw):sent.append((url,kw['json']));return Response()
    monkeypatch.setattr('share_common.notify.aiohttp.ClientSession',lambda **kw:Session())
    notices.emit('buy rejected',{'mint':'fixture-mint','reason':'price-cache-miss'},alert=True)
    runner=asyncio.create_task(notices.run())
    try:
        await asyncio.wait_for(notices.queue.join(),1)
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True)
    assert len(sent)==1 and sent[0][1]['chat_id']=='fixture-chat'
    assert 'fixture-mint' in sent[0][1]['text'] and 'price-cache-miss' in sent[0][1]['text']
