import asyncio
import base64
import json
import time
from dataclasses import replace
import pytest
from chain_common.primitives import TOKEN, TOKEN_2022, pubkey
from chain_common.rpc import Priority
from features.strategy.holder_check import HolderCheck, holder_count
from tests.helpers import address, Notices
from tests.test_dev_exit import setup


def response(mint,owners,program=TOKEN,slot=100):
    rows=[]
    for owner,amount in owners:
        raw=bytearray(165);raw[:32]=bytes(pubkey(mint));raw[32:64]=bytes(pubkey(owner))
        raw[64:72]=amount.to_bytes(8,'little');raw[108]=1
        rows.append({'pubkey':address(),'account':{'owner':program,'executable':False,
            'data':[base64.b64encode(raw).decode(),'base64'],'space':181 if program==TOKEN_2022 else 165}})
    return {'context':{'slot':slot},'value':rows}


@pytest.mark.parametrize('program',[TOKEN,TOKEN_2022])
def test_unique_positive_owners_include_pool_and_self(program):
    mint,wallet,pool=address(),address(),address()
    result=response(mint,[(wallet,10),(wallet,20),(pool,1),(address(),0)],program)
    assert holder_count(result,mint,program,wallet,100)==(2,100)


@pytest.mark.parametrize('mutation',['empty','stale','duplicate','mint','owner','bytes','state','missing-self','oversized'])
def test_unknown_counts_never_become_zero(mutation):
    mint,wallet=address(),address();result=response(mint,[(wallet,10)])
    if mutation=='empty':result['value']=[]
    if mutation=='stale':result['context']['slot']=99
    if mutation=='duplicate':result['value']*=2
    if mutation=='mint':mint=address()
    if mutation=='owner':result['value'][0]['account']['owner']=address()
    if mutation=='bytes':result['value'][0]['account']['data']=['bad','base64']
    if mutation=='state':
        raw=bytearray(base64.b64decode(result['value'][0]['account']['data'][0]));raw[108]=0
        result['value'][0]['account']['data'][0]=base64.b64encode(raw).decode()
    if mutation=='missing-self':wallet=address()
    if mutation=='oversized':result['value']*=10000
    with pytest.raises(ValueError):holder_count(result,mint,TOKEN,wallet,100)


async def prepared(config,store,count=4,program=None):
    exit,rpc,oid,route,builds=await setup(config,store)
    with store.db('trading') as c:c.execute('DELETE FROM token_dev_holdings')
    store.fill(oid,1000,100)
    cfg=exit.config;request=route.request();program=program or request['tokenProgram']
    if program!=request['tokenProgram']:
        request['tokenProgram']=program
        with store.db('trading') as c:c.execute('UPDATE order_routes SET request=? WHERE order_id=?',(json.dumps(request),oid))
    class Reader:
        calls=[]
        async def call(self,method,params):
            self.calls.append((method,params))
            assert method=='getProgramAccounts'
            return response(route.trade.mint,[(cfg.wallet_address,1000)]+[(address(),1) for _ in range(count-1)],
                            program,route.trade.slot)
    reader=Reader();checker=HolderCheck(cfg,store,reader,None,Notices(),Priority(),exit)
    checker.arm(store.order(oid),route.trade.slot)
    return checker,reader,exit,rpc,oid,builds


def due(store):
    with store.db('trading') as c:c.execute('UPDATE holder_checks SET due=?',(time.time()-1,))


@pytest.mark.asyncio
@pytest.mark.parametrize('count,expected',[(4,'triggered'),(5,'passed'),(6,'passed')])
async def test_deadline_boundary_idempotence_and_shared_sell(config,store,count,expected):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store,count)
    first=store.rows('trading','SELECT * FROM holder_checks')[0]
    await checker.once();assert reader.calls==[]
    checker.arm(store.order(oid),100)
    assert store.rows('trading','SELECT due FROM holder_checks')[0]['due']==first['due']
    due(store);await checker.once();await checker.once()
    assert len(reader.calls)==1
    assert store.rows('trading','SELECT state FROM holder_checks')[0]['state']==expected
    if expected=='triggered':
        await exit.once()
        assert builds==[1000]
        assert store.rows('trading','SELECT state,reason FROM dev_exits')[0]['state']=='submitted'
        assert any(args[0]=='holder exit submitted' for args,kw in exit.notices.items)
    else:assert not store.rows('trading','SELECT * FROM dev_exits')


@pytest.mark.asyncio
async def test_failed_public_uses_alchemy_and_supports_extended_token_accounts(config,store):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store,program=TOKEN_2022)
    class Failed:
        async def call(self,*args):raise RuntimeError('secret endpoint')
    checker.public=Failed();checker.alchemy=reader
    due(store);await checker.once()
    detail=json.loads(store.rows('trading','SELECT detail FROM holder_checks')[0]['detail'])
    assert detail['provider']=='alchemy'
    method,params=reader.calls[0]
    assert params[0]==TOKEN_2022 and not any('dataSize' in f for f in params[1]['filters'])
    assert params[1]['withContext'] and params[1]['dataSlice']['length']==165


@pytest.mark.asyncio
async def test_unknown_notifies_once_and_does_not_sell(config,store):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    async def invalid(*args):return {'context':{'slot':999},'value':[]}
    reader.call=invalid;due(store);await checker.once();await checker.once()
    assert store.rows('trading','SELECT state FROM holder_checks')[0]['state']=='unavailable'
    assert not store.rows('trading','SELECT * FROM dev_exits')
    assert len(checker.notices.items)==1 and checker.notices.items[0][0][0]=='holder check unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('winner',['dev','manual','failed'])
async def test_exit_races_recheck_after_network(config,store,winner):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    original=reader.call
    async def racing(*args):
        with store.db('trading') as c:
            if winner=='dev':c.execute("INSERT INTO dev_exits(buy_id,state,updated,reason) VALUES (?,'waiting',?,'dev-sell-100-percent')",(oid,time.time()))
            if winner=='manual':c.execute("UPDATE positions SET amount='0'")
            if winner=='failed':c.execute("UPDATE orders SET state='failed' WHERE id=?",(oid,))
        return await original(*args)
    reader.call=racing;due(store);await checker.once()
    assert store.rows('trading','SELECT state FROM holder_checks')[0]['state']=='skipped'
    assert not store.rows('trading',"SELECT * FROM dev_exits WHERE reason='holder-check'")


@pytest.mark.asyncio
async def test_restart_priority_and_disabled_do_not_repeat_or_reset_timer(config,store):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    due(store)
    restarted=HolderCheck(checker.config,store,reader,None,checker.notices,checker.priority,exit)
    checker.priority.active=1;await restarted.once();assert reader.calls==[]
    checker.priority.active=0
    restarted.config=replace(checker.config,holder_check_enabled=False);await restarted.once();assert reader.calls==[]
    restarted.config=checker.config;await restarted.once();await checker.once();assert len(reader.calls)==1


def test_policy_defaults_and_notifications(config):
    from share_common.notify import telegram_message
    assert config.holder_check_seconds==10 and config.min_holders==5 and config.holder_check_enabled
    text=telegram_message('holder exit finalized',{'mint':address(),'holders':4,'minimum':5,'mode':'live'})
    assert '人氣不足' in text and '持有人數' in text and '<code>' in text


@pytest.mark.asyncio
async def test_executor_arms_only_after_successful_confirmation(config,store):
    from trade_execution.executor import Executor
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    with store.db('trading') as c:
        c.execute('DELETE FROM holder_checks')
        c.execute("UPDATE orders SET state='submitted',reason='atomic-pump-buy-v1' WHERE id=?",(oid,))
    executor=Executor(checker.config,store,rpc,Notices(),Priority())
    executor.holder_check=checker
    rpc.status={'confirmationStatus':'processed','err':None,'slot':100}
    await executor.reconcile_once();assert not store.rows('trading','SELECT * FROM holder_checks')
    rpc.status={'confirmationStatus':'confirmed','err':None,'slot':100}
    before=time.time();await executor.reconcile_once()
    first=store.rows('trading','SELECT * FROM holder_checks')[0]
    assert before+10<=first['due']<=time.time()+10
    await executor.reconcile_once()
    assert store.rows('trading','SELECT due FROM holder_checks')[0]['due']==first['due']
    assert reader.calls==[]


@pytest.mark.asyncio
async def test_cancellation_keeps_pending_check_for_restart(config,store):
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    started=asyncio.Event()
    async def blocked(*args):started.set();await asyncio.Event().wait()
    reader.call=blocked;due(store)
    task=asyncio.create_task(checker.once());await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert store.rows('trading','SELECT state FROM holder_checks')[0]['state']=='waiting'
    assert not store.rows('trading','SELECT * FROM dev_exits')


@pytest.mark.parametrize('name,value',[
    ('SOL_POST_BUY_MIN_HOLDERS','0'),('SOL_POST_BUY_MIN_HOLDERS','1001'),
    ('SOL_POST_BUY_HOLDER_CHECK_SECONDS','0'),('SOL_POST_BUY_HOLDER_CHECK_SECONDS','301')])
def test_invalid_holder_policy_rejected(name,value):
    from share_common.config import load
    with pytest.raises(ValueError):load(env={name:value})


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback',[False,True])
async def test_holder_query_reaches_provider_through_production_background_wrapper(config,store,fallback):
    from chain_common.background_rpc import BackgroundRpc
    from chain_common.rpc import RpcError
    checker,reader,exit,rpc,oid,builds=await prepared(config,store,count=5)
    calls=[]
    class Public:
        async def call(self,method,params):
            calls.append('public')
            if fallback:raise RpcError('HTTP-429')
            return await reader.call(method,params)
    class Alchemy:
        async def call(self,method,params):
            calls.append('alchemy')
            return await reader.call(method,params)
    checker.public=BackgroundRpc(Public(),checker.priority,1000)
    checker.alchemy=BackgroundRpc(Alchemy(),checker.priority,1000)
    due(store);await checker.once()
    job=store.rows('trading','SELECT * FROM holder_checks')[0]
    assert job['state']=='passed'
    assert calls==(['public','alchemy'] if fallback else ['public'])
    assert json.loads(job['detail'])['provider']==('alchemy' if fallback else 'public')
    assert len(reader.calls)==1 and not builds
    assert not checker.notices.items


@pytest.mark.asyncio
async def test_background_wrapper_still_rejects_transaction_submission():
    from chain_common.background_rpc import BackgroundRpc
    class Client:
        async def call(self,*args):raise AssertionError('must not reach provider')
    with pytest.raises(ValueError,match='background-rpc-read-method-required'):
        await BackgroundRpc(Client(),Priority()).call('sendTransaction',[])


@pytest.mark.asyncio
async def test_provider_errors_are_retained_without_credentials_or_unwanted_sell(config,store):
    from chain_common.background_rpc import BackgroundRpc
    from chain_common.rpc import RpcError
    from share_common.notify import telegram_message
    checker,reader,exit,rpc,oid,builds=await prepared(config,store)
    class Public:
        async def call(self,*args):raise RpcError('HTTP-429')
    class Alchemy:
        async def call(self,*args):raise RpcError('getProgramAccounts:RPC--32010')
    checker.public=BackgroundRpc(Public(),checker.priority,1000)
    checker.alchemy=BackgroundRpc(Alchemy(),checker.priority,1000)
    due(store);await checker.once();await checker.once()
    job=store.rows('trading','SELECT * FROM holder_checks')[0]
    detail=json.loads(job['detail']);failures=detail['failures']
    assert job['state']=='unavailable'
    assert [r['reason'] for r in failures]==['holder-http-error','holder-rpc-error']
    assert failures[0]['http_status']==429 and failures[1]['rpc_code']==-32010
    assert len(checker.notices.items)==1 and not store.rows('trading','SELECT * FROM dev_exits')
    text=telegram_message(*checker.notices.items[0][0])
    assert '公共 RPC' in text and 'HTTP 429' in text and 'Alchemy' in text and 'RPC code -32010' in text
    audit=json.loads(store.rows('audit',"SELECT detail FROM decisions WHERE stage='holder-check'")[0]['detail'])
    assert audit['failures']==failures


def test_holder_failure_classification_and_notification_are_allowlisted():
    from chain_common.rpc import RpcError
    from features.strategy.holder_diagnostics import failure,lines
    for exc,expected in [(TimeoutError('secret'),'holder-timeout'),
            (RpcError('getProgramAccounts:TimeoutError'),'holder-timeout'),
            (ValueError('background-rpc-read-method-required'),'background-rpc-read-method-required'),
            (RpcError('endpoint-cooling'),'holder-endpoint-cooling'),
            (ValueError('holder-buy-not-visible'),'holder-buy-not-visible'),
            (RpcError('getProgramAccounts:RPC-https://secret'),'holder-rpc-failed')]:
        row=failure(exc,'public',10)
        assert row['reason']==expected and 'secret' not in json.dumps(row)
    assert failure(KeyError('secret'),'alchemy',10,'validation')['reason']=='holder-response-invalid'
    assert lines({'failures':[{'provider':[],'reason':'secret'},None,
                 {'provider':'public','reason':'https://secret'}]})==[]
    text=' '.join(lines({'failures':[{'provider':'public','reason':'holder-timeout',
        'http_status':'secret','rpc_code':True,'elapsed_ms':float('nan'),'url':'secret'}]}))
    assert 'secret' not in text and 'nan' not in text and 'RPC code' not in text
