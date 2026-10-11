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
