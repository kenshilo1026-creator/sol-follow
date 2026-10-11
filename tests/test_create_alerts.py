"""Offline dry create notifications independent of entry eligibility."""
import asyncio
from dataclasses import replace
from decimal import Decimal
import time

import pytest

from chain_common.transaction import Tx
from features.database.storage import Store
from features.runtime.service import Service
from launchpads.create import decode
from share_common.notify import Notices as TelegramNotices, message
from tests.create_snipe_helpers import setup_create, token_snapshot
from tests.helpers import Notices
from tests.test_create_decoders import pump, launchlab


def alerts(notices):
    return [args[1] for args,kw in notices.items if args[0]=='dry create detected' and kw.get('alert')]


def standalone(config,store,raw):
    raw['blockTime']=int(time.time())
    service=Service(config,store);service.notices=Notices()
    item=decode(Tx(raw))[0]
    store.enqueue([item.signature])
    class RPC:
        async def transaction(self,*args):return raw
        async def call(self,*args):raise AssertionError('create notification requires no extra RPC')
    return service,item,{'signature':item.signature},RPC()


@pytest.mark.asyncio
@pytest.mark.parametrize('build',[pump,lambda:pump(v2=False),launchlab,lambda:pump(cpi=True)])
async def test_every_supported_non_hotlisted_pure_create_alerts(config,store,build):
    service,item,row,rpc=standalone(config,store,build())
    await service.process(row,rpc)
    detail=alerts(service.notices)[0]
    assert detail['event']==item.event and detail['mint']==item.mint
    assert detail['user']==item.user and detail['creator']==item.creator
    assert detail['commitment']=='confirmed'
    assert not store.rows('trading','SELECT * FROM orders')
    await service.process(row,rpc)
    assert len(alerts(service.notices))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('quantity_state',['allowed','blocked','unavailable'])
async def test_processed_create_alerts_even_when_quantity_blocks(config,store,quantity_state):
    cfg=replace(config,max_dev_holding_tokens=Decimal('60000000'))
    case=setup_create(cfg,store)
    if quantity_state!='unavailable':
        token_snapshot(case.raw,case.item,0 if quantity_state=='allowed' else 60000001*10**case.item.decimals)
        store.stream_transaction(case.raw)
    await case.service.process(case.row,case.rpc)
    assert len(alerts(case.service.notices))==1
    assert alerts(case.service.notices)[0]['commitment']=='processed'
    assert case.service.dev_holdings.row(case.item.mint)['state']==quantity_state
    assert bool(case.calls)==(quantity_state=='allowed')


@pytest.mark.asyncio
async def test_processed_confirmed_and_restart_share_durable_dedupe(config,store):
    case=setup_create(config,store)
    await case.service.process(case.row,case.rpc)
    assert len(alerts(case.service.notices))==1
    case.service.proofs.confirmed(case.item.signature,'confirmed')
    case.raw.pop('_stream');case.raw['blockTime']=int(time.time())
    await case.service.process(case.row,case.rpc)
    assert len(alerts(case.service.notices))==1
    reopened=Store(config.data)
    service=Service(config,reopened);service.notices=Notices()
    await service.process(case.row,case.rpc)
    assert alerts(service.notices)==[]
    assert len(reopened.rows('funding','SELECT * FROM dry_create_alerts'))==1


@pytest.mark.asyncio
async def test_two_creates_in_one_transaction_each_alert(config,store):
    raw=pump();second=pump()
    keys=raw['transaction']['message']['accountKeys']
    known={r['pubkey'] for r in keys}
    keys.extend(r for r in second['transaction']['message']['accountKeys'] if r['pubkey'] not in known)
    raw['transaction']['message']['instructions']+=second['transaction']['message']['instructions']
    raw['meta']['preBalances']=raw['meta']['postBalances']=[10**9]*len(keys)
    service,item,row,rpc=standalone(config,store,raw)
    assert len(decode(Tx(raw)))==2
    await service.process(row,rpc)
    assert len(alerts(service.notices))==2
    assert len({d['event'] for d in alerts(service.notices)})==2
    await service.process(row,rpc)
    assert len(alerts(service.notices))==2


@pytest.mark.asyncio
async def test_alert_precedes_funding_retry_and_is_not_duplicated(config,store,monkeypatch):
    service,item,row,rpc=standalone(config,store,pump())
    monkeypatch.setattr('features.runtime.service.funding_decode',lambda *args:[object()])
    monkeypatch.setattr('features.runtime.service.funding_candidates',lambda *args:None)
    async def fail(*args):raise RuntimeError('funding-temporarily-unavailable')
    service.qualify=fail
    for _ in range(2):
        with pytest.raises(RuntimeError,match='funding-temporarily-unavailable'):
            await service.process(row,rpc)
    assert len(alerts(service.notices))==1


@pytest.mark.asyncio
async def test_queue_full_releases_claim_for_retry(config,store):
    service,item,row,rpc=standalone(config,store,pump())
    notices=TelegramNotices(config,store);notices.queue=asyncio.Queue(maxsize=1)
    notices.emit('fixture',{})
    service.notices=notices
    with pytest.raises(RuntimeError,match='create-notification-queue-full'):
        await service.process(row,rpc)
    assert store.rows('funding','SELECT * FROM dry_create_alerts')==[]
    notices.queue.get_nowait();notices.queue.task_done()
    await service.process(row,rpc)
    kind,detail,alert=notices.queue.get_nowait()
    assert kind=='dry create detected' and detail['mint']==item.mint and alert
    assert len(store.rows('funding','SELECT * FROM dry_create_alerts'))==1


@pytest.mark.asyncio
async def test_live_mode_does_not_emit_dry_create_alert(config,store):
    service,item,row,rpc=standalone(replace(config,dry_run=False),store,pump())
    await service.process(row,rpc)
    assert alerts(service.notices)==[]
    assert store.rows('funding','SELECT * FROM dry_create_alerts')==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['failed','unknown'])
async def test_failed_or_unknown_instructions_do_not_claim_create(config,store,kind):
    raw=pump()
    service,item,row,rpc=standalone(config,store,raw)
    if kind=='failed':raw['meta']['err']={'InstructionError':[0,'fixture']}
    else:raw['transaction']['message']['instructions'][0]['data']=''
    await service.process(row,rpc)
    assert alerts(service.notices)==[]
    assert store.rows('funding','SELECT * FROM dry_create_alerts')==[]


@pytest.mark.asyncio
async def test_detected_create_reaches_telegram_without_network(config,store,monkeypatch):
    sent=[]
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,**kw):sent.append(kw['json']);return Response()
    monkeypatch.setattr('share_common.notify.aiohttp.ClientSession',lambda **kw:Session())
    cfg=replace(config,telegram_token='fixture-token',telegram_chat='fixture-chat')
    service,item,row,rpc=standalone(cfg,store,pump())
    notices=TelegramNotices(cfg,store);service.notices=notices
    async with notices.running():await service.process(row,rpc)
    assert len(sent)==1 and sent[0]['chat_id']=='fixture-chat'
    text=sent[0]['text']
    for value in (item.mint,item.user,item.signature,item.name,item.symbol,'Dry run','confirmed'):
        assert value in text
    detail={**item.dict(),'commitment':'processed'}
    assert '尚未確認' in message('dry create detected',detail)


@pytest.mark.asyncio
async def test_retention_keeps_recent_create_alerts(config,store):
    from features.database.maintenance import Maintenance
    from chain_common.rpc import Priority
    assert store.claim_create_alert('recent')
    assert store.claim_create_alert('old')
    with store.db('funding') as db:
        db.execute('UPDATE dry_create_alerts SET queued=? WHERE event=?',
                   (time.time()-max(config.backfill_age*2,86400)-60,'old'))
    await Maintenance(config,store,Priority(),Notices()).pass_once()
    assert store.rows('funding','SELECT event FROM dry_create_alerts')==[{'event':'recent'}]
    assert not store.claim_create_alert('recent')
