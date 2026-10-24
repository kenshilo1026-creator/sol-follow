"""Minute-based launch age gating; all transaction fixtures and RPCs are offline."""
from dataclasses import replace
import json
import time

import pytest

from chain_common.transaction import Tx
from features.database.storage import Store
from features.funding.decoder import Funding
from features.strategy.dev_create import CreateSignals
from launchpads.create import decode
from share_common.config import load
from tests.create_snipe_helpers import setup_create
from tests.helpers import address
from tests.test_create_decoders import pump,launchlab


@pytest.mark.parametrize('minutes,seconds',[('0',0),('10',600),('0.5',30),('1.25',75),('43200',2592000)])
def test_minute_setting_units(minutes,seconds):
    cfg=load(env={'SOL_BUY_ONLY_AGED_MINUTES':minutes})
    assert cfg.min_funding_age_seconds==seconds
    assert cfg.buy_only_aged_minutes*60==seconds


@pytest.mark.parametrize('value',['-1','NaN','Infinity','-Infinity','abc','','43201','0.0001'])
def test_invalid_minute_setting_rejected(value):
    with pytest.raises(ValueError,match='invalid-aged-funding-minutes'):
        load(env={'SOL_BUY_ONLY_AGED_MINUTES':value})


def test_local_dotenv_default_and_environment_override(tmp_path):
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}')
    assert load(tmp_path,env={}).min_funding_age_seconds==0
    (tmp_path/'.env').write_text('SOL_BUY_ONLY_AGED_MINUTES=7.5\n')
    assert load(tmp_path,env={}).min_funding_age_seconds==450
    assert load(tmp_path,env={'SOL_BUY_ONLY_AGED_MINUTES':'0'}).min_funding_age_seconds==0


def launch(factory=pump):
    raw=factory();now=int(time.time());raw['blockTime']=now
    tx=Tx(raw)
    return raw,tx,decode(tx)[0],now


def deposit(store,item,stamp,now,*,slot=None,provider='cex:test',asset='SOL'):
    slot=item.slot-1 if slot is None else slot
    value=Funding('funding-'+str(slot), 'deposit-'+str(slot),item.user,address(),provider,
                  10**9,slot,stamp,asset=asset)
    assert store.admit(value,3600,now)
    return value


def age_decisions(store):
    return store.rows('audit',"SELECT * FROM decisions WHERE reason='funding-age'")


@pytest.mark.parametrize('factory',[pump,launchlab])
@pytest.mark.parametrize('provider,asset',[('cex:test','SOL'),('cex:test','USDC'),('privacy-cash','SOL')])
@pytest.mark.parametrize('required,age,allowed',[(0,1,True),(600,599,False),(600,600,True),
                                             (600,601,True),(30,30,True)])
def test_create_age_boundary_and_all_sources(config,store,factory,provider,asset,required,age,allowed):
    raw,tx,item,now=launch(factory)
    funding=deposit(store,item,now-age,now,provider=provider,asset=asset)
    assert item.user in store.hotlist(now)  # Qualification does not wait for age.
    signals=CreateSignals(store,replace(config,min_funding_age_seconds=required),started=0)
    route=signals.prepare(item,tx,True,now=now)
    assert bool(route)==allowed
    assert store.hotlist(now)=={}  # Even an age rejection consumes this create.
    assert len(store.rows('funding','SELECT * FROM launch_creates'))==1
    if allowed:
        oid=signals.reserve(route)
        assert oid and store.order(oid)['amount']==str(config.buy_amount)
        assert not age_decisions(store)
    else:
        assert not store.rows('trading','SELECT * FROM orders')
        detail=json.loads(age_decisions(store)[0]['detail'])
        assert detail['funding_event']==funding.event
        assert detail['funded_time']==now-age
        assert detail['funding_age_minutes']==age/60
        assert detail['required_funding_age_minutes']=='10'


@pytest.mark.parametrize('recent_first',[False,True])
def test_latest_qualified_deposit_resets_age_even_when_older_is_mature(config,store,recent_first):
    raw,tx,item,now=launch()
    rows=[(item.slot-2,now-1200),(item.slot-1,now-10)]
    if recent_first:rows.reverse()
    for slot,stamp in rows:deposit(store,item,stamp,now,slot=slot)
    signals=CreateSignals(store,replace(config,min_funding_age_seconds=600),started=0)
    assert signals.prepare(item,tx,True,now=now) is None
    detail=json.loads(age_decisions(store)[0]['detail'])
    assert detail['funding_event']=='funding-'+str(item.slot-1)
    assert detail['funded_time']==now-10


@pytest.mark.parametrize('bad_stamp',[0,-1])
def test_unknown_legacy_timestamp_does_not_pass_enabled_gate(config,store,bad_stamp):
    raw,tx,item,now=launch()
    deposit(store,item,now-1200,now)
    with store.db('funding') as db:db.execute('UPDATE funding SET time=?',(bad_stamp,))
    signals=CreateSignals(store,replace(config,min_funding_age_seconds=600),started=0)
    assert signals.prepare(item,tx,True,now=now) is None
    assert store.hotlist(now)=={}
    assert json.loads(age_decisions(store)[0]['detail'])['funding_age_minutes'] is None


@pytest.mark.parametrize('case',['same-slot','later-slot','expired'])
def test_mature_age_does_not_bypass_slot_or_expiry_checks(config,store,case):
    raw,tx,item,now=launch()
    slot=item.slot if case=='same-slot' else item.slot+1 if case=='later-slot' else item.slot-1
    deposit(store,item,now-1200,now,slot=slot)
    if case=='expired':
        with store.db('funding') as db:db.execute('UPDATE funding SET expires=?',(now,))
    signals=CreateSignals(store,replace(config,min_funding_age_seconds=600),started=0)
    assert signals.prepare(item,tx,True,now=now) is None
    assert not store.rows('trading','SELECT * FROM orders')


def test_rejected_create_never_buys_later_or_after_restart(config,store):
    raw,tx,item,now=launch();deposit(store,item,now-599,now)
    cfg=replace(config,min_funding_age_seconds=600)
    signals=CreateSignals(store,cfg,started=0)
    assert signals.prepare(item,tx,True,now=now) is None
    # Still fresh at +2s and funding now mature, but consumption forbids replay.
    assert signals.prepare(item,tx,True,now=now+2) is None
    reopened=Store(config.data)
    assert CreateSignals(reopened,cfg,started=0).prepare(item,tx,True,now=now+2) is None
    assert not reopened.rows('trading','SELECT * FROM orders')


@pytest.mark.asyncio
@pytest.mark.parametrize('processed',[False,True])
@pytest.mark.parametrize('case_name',['missing','unknown-time','expired','same-slot','latest-too-young','other-wallet'])
async def test_dry_alert_requires_mature_eligible_dev_funding(config,store,processed,case_name):
    case=setup_create(replace(config,min_funding_age_seconds=600),store,processed=processed)
    now=int(time.time())
    with store.db('funding') as db:
        db.execute('UPDATE funding SET time=?',(now-1200,))
        if case_name=='missing':db.execute('DELETE FROM funding')
        elif case_name=='unknown-time':db.execute('UPDATE funding SET time=0')
        elif case_name=='expired':db.execute('UPDATE funding SET expires=?',(now,))
        elif case_name=='same-slot':db.execute('UPDATE funding SET slot=?',(case.item.slot,))
        elif case_name=='other-wallet':db.execute('UPDATE funding SET wallet=?',(address(),))
        elif case_name=='latest-too-young':db.execute('UPDATE funding SET slot=?',(case.item.slot-2,))
    if case_name=='latest-too-young':
        deposit(store,case.item,now-10,now)
    await case.service.process(case.row,case.rpc)
    assert not [args for args,kw in case.service.notices.items
                if args[0]=='dry create detected' and kw.get('alert')]
    assert store.rows('funding','SELECT * FROM dry_create_alerts')==[]
    assert not case.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('processed',[False,True])
@pytest.mark.parametrize('age,allowed',[(10,False),(599,False),(600,True),(1200,True)])
async def test_service_age_gate_controls_build_and_dry_create_alert(config,store,processed,age,allowed,monkeypatch):
    now=int(time.time())
    monkeypatch.setattr('time.time',lambda:now)
    case=setup_create(replace(config,min_funding_age_seconds=600),store,processed=processed)
    with store.db('funding') as db:db.execute('UPDATE funding SET time=?',(int(time.time())-age,))
    await case.service.process(case.row,case.rpc)
    assert bool(case.calls)==allowed
    assert not store.hotlist(time.time())
    alerts=[args for args,kw in case.service.notices.items if args[0]=='dry create detected' and kw.get('alert')]
    assert len(alerts)==int(allowed)
    if not allowed:
        assert not store.rows('trading','SELECT * FROM orders')
        assert not store.rows('trading','SELECT * FROM token_dev_holdings')
        assert age_decisions(store)
        if processed:
            case.service.proofs.confirmed(case.item.signature,'confirmed')
            case.raw.pop('_stream');case.raw['blockTime']=int(time.time())
        # Later maturity/confirmation must not revive this rejected create alert.
        monkeypatch.setattr('time.time',lambda:now+601)
        await case.service.process(case.row,case.rpc)
        assert not case.calls
        assert not store.rows('trading','SELECT * FROM orders')
        assert not [args for args,kw in case.service.notices.items
                    if args[0]=='dry create detected' and kw.get('alert')]
