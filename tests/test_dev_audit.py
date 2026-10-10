import json
import time
from dataclasses import replace
from pathlib import Path
import pytest
from features.audit.dev_audit import report,main
from features.audit.writer import record,funding_candidates
from features.database.storage import Store
from features.strategy.signals import Signals
from features.runtime.service import Service
from features.funding.decoder import Funding
from features.funding.activity_filter import HistoryPending
from chain_common.primitives import SYSTEM
from chain_common.transaction import Tx
from tests.helpers import address,transaction
from tests.test_signals import fund,trade


def test_candidate_rejection_is_wallet_indexed(config,store):
    source,wallet=address(),address()
    ix={'programId':SYSTEM,'parsed':{'type':'transfer','info':{'source':source,'destination':wallet,'lamports':1}}}
    raw=transaction([source,wallet],[source],[ix],pre=[10000,0],post=[4999,1])
    cfg=replace(config,cex={source:'test'},min_funding=100)
    funding_candidates(store,Tx(raw),cfg,[])
    funding_candidates(store,Tx(raw),cfg,[])
    result=report(config.data,'wallet',wallet=wallet)
    assert [r['reason'] for r in result['decisions']]==['funding-below-minimum']
    assert result['decisions'][0]['detail']['minimum_lamports']=='100'
    assert not result['hotlist_active_now']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason',[('over-cap','prelaunch-activity-too-high'),
    ('missing','wallet-account-missing'),('pending','wallet-history-check-incomplete')])
async def test_qualification_denials_and_pending_are_recorded(config,store,monkeypatch,mode,reason):
    now=int(time.time());wallet=address()
    item=Funding('deposit','deposit',wallet,address(),'cex:test',10**9,10,now)
    class Rpc:
        async def call(self,*args):return {'value':None if mode=='missing' else {'owner':SYSTEM,'executable':False,'data':['','base64']}}
    async def activity(*args):
        if mode=='pending':raise HistoryPending(reason)
        return False,reason
    monkeypatch.setattr('features.runtime.service.qualify_activity',activity)
    service=Service(config,store)
    if mode=='pending':
        with pytest.raises(HistoryPending):await service.qualify(item,Rpc())
    else:assert not await service.qualify(item,Rpc())
    result=report(config.data,'wallet',wallet=wallet)
    verdict=next(r for r in result['decisions'] if r['stage']=='qualification')
    assert verdict['reason']==reason
    assert verdict['outcome']==('pending' if mode=='pending' else 'blocked')


def test_n_wallet_evidence_survives_cleanup_and_live_dry_are_separate(config,store):
    now=time.time();wallets=[address(),address()];mint=address()
    cfg=replace(config,n=2,dry_run=False)
    engine=Signals(store,cfg,started=now-1)
    for wallet in wallets:fund(store,wallet,now)
    assert engine.observe(trade(wallets[0],mint,'one',now),now) is None
    waiting=report(config.data,'wallet',wallet=wallets[0])['decisions'][0]
    assert waiting['reason']=='wallet-threshold-not-reached' and waiting['detail']['count']==1
    oid=engine.observe(trade(wallets[1],mint,'two',now),now)
    store.update_order(oid,state='confirmed',signature='our-buy',raw='SECRET_SIGNED_BYTES')
    with store.db('trading') as c:c.execute('DELETE FROM votes')
    dry=store.reserve(replace(cfg,dry_run=True),address(),address(),1)
    store.update_order(dry,state='dry-simulated')
    failed=store.reserve(cfg,address(),address(),1);store.fail_order(failed,'simulation-rejected')
    result=report(config.data,'followed',now=now+1)
    assert len(result['orders'])==1
    assert {r['wallet'] for r in result['orders'][0]['sources']}==set(wallets)
    assert result['orders'][0]['category']=='confirmed-buy-not-finalized'
    assert 'SECRET_SIGNED_BYTES' not in json.dumps(result)
    assert len(report(config.data,'followed',all_states=True)['orders'])==3
    assert report(config.data,'wallet',wallet=wallets[0])['orders'][0]['id']==oid
    with store.db('trading') as c:c.execute('UPDATE orders SET created=? WHERE id=?',(now-10*3600,oid))
    assert not report(config.data,'followed',hours=2)['orders']


def test_legacy_orders_not_guessed_and_missing_database_not_created(tmp_path,config,store):
    missing=tmp_path/'does-not-exist'
    r=report(missing,'wallet',wallet=address())
    assert not missing.exists() and len(r['missing_databases'])==3
    oid=store.reserve(replace(config,dry_run=False),address(),address(),1)
    store.update_order(oid,state='finalized')
    r=report(config.data,'followed')
    assert r['orders'][0]['sources']==[]
    assert r['orders'][0]['source_mapping'].startswith('unavailable')
    with store.db('audit') as c:c.execute('DROP TABLE decisions')
    assert not report(config.data,'wallet',wallet=address())['decisions']


def test_cli_reads_only_and_no_secret_config_required(config,store,capsys):
    w=address();record(store,'signal','blocked','source-buy-below-minimum',wallet=w,signature='sig')
    before={p.name:p.read_bytes() for p in config.data.glob('*.sqlite3')}
    main(['dev',w,'--data',str(config.data),'--hours','2','--json'])
    result=json.loads(capsys.readouterr().out)
    assert result['wallet']==w and result['decisions'][0]['reason']=='source-buy-below-minimum'
    assert before=={p.name:p.read_bytes() for p in config.data.glob('*.sqlite3')}
    with pytest.raises(SystemExit):main(['followed','--hours','NaN'])


def test_writer_failure_does_not_change_strategy_flow(caplog):
    class Broken:
        def db(self,*args):raise OSError('do-not-print-secret-url')
    record(Broken(),'signal','blocked','test',wallet=address(),signature='s')
    assert 'audit unavailable' in caplog.text and 'do-not-print-secret-url' not in caplog.text


def test_bounded_report_marks_limit_and_legacy_json_is_safe(config,store):
    w=address()
    for n in range(3):record(store,'signal','waiting','wallet-threshold-not-reached',wallet=w,signature=str(n))
    with store.db('audit') as c:c.execute('INSERT INTO audit(time,kind,detail) VALUES (?,?,?)',(time.time(),'legacy','{invalid'))
    r=report(config.data,'wallet',wallet=w,limit=2)
    assert len(r['decisions'])==2 and 'decisions' in r['at_limit_sections']
