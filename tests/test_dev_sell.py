"""Offline sale attribution, full-exit policy and durable event delivery."""
import asyncio
import copy
import json
import time
from pathlib import Path
from dataclasses import replace
import pytest
from chain_common.transaction import Tx
from chain_common.primitives import TOKEN,TOKEN_2022,WSOL,ata,b58encode,discriminator
from launchpads import pump_fun,stonk
from features.strategy.dev_sell import DevSell,sold_amount,sell_leg
from features.funding.discovery import Discovery
from features.database.storage import Store
from features.strategy.create_dev_gate import permitted
from tests.helpers import address,transaction,Notices
from tests.test_dev_exit import setup as exit_setup


def sale(kind='pump',*,dev=None,mint=None,slot=500000000,amount=1,sig='dev-sale'):
    dev=dev or address();mint=mint or address();quote=WSOL
    source=ata(dev,mint,TOKEN);pool=stonk.pool_address(mint,quote) if kind=='stonk' else pump_fun.curve_address(mint)
    vault=ata(pool,mint,TOKEN)
    a=[address() for _ in range(26)]
    if kind=='pump':
        program=pump_fun.PROGRAM;method='sell';a=a[:14]
        for n,key in {2:mint,3:pool,4:vault,5:source,6:dev,9:TOKEN,11:program}.items():a[n]=key
    elif kind=='pump-v2':
        program=pump_fun.PROGRAM;method='sell_v2'
        for n,key in {1:mint,2:quote,3:TOKEN,10:pool,11:vault,13:dev,14:source,25:program}.items():a[n]=key
    else:
        program=stonk.PROGRAM;method='sell_exact_in';a=a[:18]
        for n,key in {0:dev,3:stonk.PLATFORM,4:pool,5:source,7:vault,9:mint,10:quote,11:TOKEN,14:program}.items():a[n]=key
    keys=list(dict.fromkeys([dev,mint,source,vault,TOKEN,program,*a]))
    def balance(key,owner,n):return {'accountIndex':keys.index(key),'owner':owner,'mint':mint,'uiTokenAmount':{'amount':str(n)}}
    ix={'programId':program,'accounts':a,'data':b58encode(discriminator('global',method)+amount.to_bytes(8,'little')+bytes(8)), 'stackHeight':1}
    moved={'programId':TOKEN,'stackHeight':2,'parsed':{'type':'transferChecked','info':
        {'source':source,'destination':vault,'authority':dev,'mint':mint,'tokenAmount':{'amount':str(amount),'decimals':6}}}}
    raw=transaction(keys,{dev},[ix],inner=[{'index':0,'instructions':[moved]}],
        pre_tokens=[balance(source,dev,1000),balance(vault,pool,2000)],
        post_tokens=[balance(source,dev,1000-amount),balance(vault,pool,2000+amount)],slot=slot,when=int(time.time()),sig=sig)
    return raw,dev,mint


@pytest.mark.parametrize('kind',['pump','pump-v2','stonk'])
def test_one_raw_token_is_a_sale(kind):
    raw,dev,mint=sale(kind)
    assert sold_amount(Tx(raw),dev,mint)==1
    assert sold_amount(Tx(raw),address(),mint)==0
    assert sold_amount(Tx(raw),dev,address())==0


@pytest.mark.parametrize('name,amount',[('pumpswap_sell_reference',14213352207018),('graduated_sell_reference',19222990878600)])
def test_recorded_chain_sales_including_aggregator_cpi(name,amount):
    raw=json.loads((Path(__file__).parent/'fixtures'/f'{name}.json').read_text(encoding='utf-8'));tx=Tx(raw)
    matched=[]
    for path,ix in tx.instructions():
        for mint in {r[1] for r in tx.tokens('pre').values()}:
            leg=sell_leg(tx,ix,mint)
            if leg:matched.append((path,tx.tokens('pre')[leg[0]][0],mint))
    assert len(matched)==1
    path,dev,mint=matched[0]
    assert sold_amount(tx,dev,mint)==amount
    # A sibling transfer at the same CPI depth does not belong to this sale.
    if '.' in path:
        top,index=map(int,path.split('.'));bad=copy.deepcopy(raw)
        for group in bad['meta']['innerInstructions']:
            if group['index']==top:
                for child in group['instructions'][index+1:]:child['stackHeight']=2
        assert sold_amount(Tx(bad),dev,mint)==0


@pytest.mark.parametrize('change',['transfer-only','buy','burn','other-owner','other-vault','sibling'])
def test_non_sales_never_trigger(change):
    raw,dev,mint=sale();inner=raw['meta']['innerInstructions'][0]['instructions']
    if change=='transfer-only':raw['transaction']['message']['instructions']=inner;raw['meta']['innerInstructions']=[]
    if change=='buy':raw['transaction']['message']['instructions'][0]['data']=b58encode(discriminator('global','buy')+bytes(16))
    if change=='burn':inner[0]['parsed']['type']='burn'
    if change=='other-owner':
        for key in ('preTokenBalances','postTokenBalances'):raw['meta'][key][0]['owner']=address()
    if change=='other-vault':inner[0]['parsed']['info']['destination']=address()
    if change=='sibling':
        raw['transaction']['message']['instructions'].append(inner[0]);raw['meta']['innerInstructions']=[]
    assert sold_amount(Tx(raw),dev,mint)==0


async def armed(config,store,remaining=1000):
    exit,rpc,oid,route,builds=await exit_setup(config,store)
    dev=address();raw,_,_=sale(dev=dev,mint=route.trade.mint,slot=route.trade.slot+1)
    with store.db('trading') as c:
        c.execute('DELETE FROM token_dev_holdings')
        c.execute('DELETE FROM order_wallets WHERE order_id=?',(oid,))
        c.execute('INSERT INTO order_wallets VALUES (?,?,?,?)',(oid,dev,'create',route.trade.slot))
        c.execute('INSERT OR REPLACE INTO dev_sell_watches VALUES (?,?,?)',(oid,dev,route.trade.slot))
    store.fill(oid,1000,100)
    with store.db('trading') as c:c.execute('UPDATE positions SET amount=?',(str(remaining),))
    class Reader:
        value=raw;calls=0
        async def transaction(self,sig,commitment=None):
            self.calls+=1;assert commitment=='confirmed';return self.value
    reader=Reader();worker=DevSell(exit.config,store,reader,Notices(),exit)
    worker.watch_addresses()
    return worker,exit,reader,oid,dev,raw,builds


@pytest.mark.asyncio
async def test_any_sale_exits_all_remaining_once_without_buy_receipt_rpc(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store,remaining=700)
    signature=raw['transaction']['signatures'][0]
    for account in worker.watch_addresses():worker.market_event(account,signature,raw['slot'])
    await worker.once()
    assert exit.wake.is_set() and reader.calls==1
    assert store.rows('trading','SELECT state FROM dev_sell_events')==[{'state':'triggered'}]
    await exit.once()
    assert builds==[700]
    job=store.rows('trading','SELECT * FROM dev_exits')[0]
    assert job['state']=='submitted' and job['reason']=='dev-sell-100-percent'
    assert not [c for c in exit.rpc.calls if c[0]=='transaction']
    worker.market_event(dev,signature,raw['slot']);await worker.once();await exit.once()
    assert builds==[700] and worker.watch_addresses()==set()


@pytest.mark.asyncio
async def test_inbox_survives_restart_and_bypasses_funding_queue(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    discovery=Discovery(exit.config,store,None,Notices(),sources_only=True)
    discovery.market_watch=worker.watch_addresses;discovery.market_event=worker.market_event
    discovery.refresh();assert dev in discovery.addresses
    discovery.notification({'params':{'subscription':77,'result':{'context':{'slot':raw['slot']},
        'value':{'signature':raw['transaction']['signatures'][0],'err':None}}}},{dev:77})
    assert discovery.added==0
    restarted=DevSell(exit.config,Store(config.data),reader,Notices(),exit)
    await restarted.once();await exit.once();assert builds==[1000]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['failed','ordinary-transfer','wrong-signature','missing'])
async def test_bad_receipt_cannot_arm_exit(config,store,bad):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    worker.market_event(dev,'dev-sale',raw['slot'])
    if bad=='failed':reader.value['meta']['err']={'InstructionError':[0,'failure']}
    if bad=='ordinary-transfer':reader.value['transaction']['message']['instructions']=[]
    if bad=='wrong-signature':reader.value['transaction']['signatures']=['other']
    if bad=='missing':reader.value=None
    await worker.once();await exit.once()
    assert not builds and not store.rows('trading','SELECT * FROM dev_exits')


@pytest.mark.asyncio
async def test_pending_take_profit_must_finish_before_full_exit(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    buy=store.order(oid)
    sell=store.reserve(exit.config,buy['mint'],buy['pool'],200,'sell','tp')
    store.update_order(sell,state='unknown')
    worker.market_event(dev,'dev-sale',raw['slot']);await worker.once();await exit.once()
    assert not builds
    with store.db('trading') as c:
        c.execute("UPDATE orders SET state='finalized' WHERE id=?",(sell,))
        c.execute("UPDATE positions SET amount='800'")
    await exit.once();assert builds==[800]


@pytest.mark.asyncio
async def test_receipt_retry_final_attempt_at_ten_minutes_then_stops(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    worker.market_event(dev,'dev-sale',raw['slot']);reader.value=None
    with store.db('trading') as c:c.execute('UPDATE dev_sell_events SET first_seen=?',(time.time()-601,))
    await worker.once();await worker.once()
    assert reader.calls==1 and store.rows('trading','SELECT state FROM dev_sell_events')[0]['state']=='stopped'
    assert worker.notices.items[-1][0][0]=='dev sell check stopped' and not builds


@pytest.mark.asyncio
async def test_dry_run_detects_but_never_sends(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    exit.config=replace(exit.config,dry_run=True);worker.config=exit.config
    with store.db('trading') as c:
        c.execute("UPDATE orders SET mode='dry'")
        c.execute("UPDATE positions SET mode='dry'")
    worker.market_event(dev,'dev-sale',raw['slot']);await worker.once();await exit.once()
    assert not builds and store.rows('trading','SELECT state FROM dev_exits')[0]['state']=='dry-skipped'


@pytest.mark.asyncio
async def test_legacy_buying_wallet_is_not_assumed_to_be_dev(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    with store.db('trading') as c:c.execute('DELETE FROM dev_sell_watches')
    assert worker.watch_addresses()==set()
    buy=store.order(oid)
    with store.db('funding') as c:
        c.execute("INSERT INTO launch_creates(event,signature,mint,user,slot,detail) VALUES ('create','create',?,?,?,'{}')",
                  (buy['mint'],dev,raw['slot']-1))
    assert dev in worker.watch_addresses()
    assert store.rows('trading','SELECT dev FROM dev_sell_watches')[0]['dev']==dev


@pytest.mark.asyncio
async def test_proven_sale_blocks_unsigned_create_buy(config,store):
    from types import SimpleNamespace
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    route=SimpleNamespace(trade=SimpleNamespace(mint=store.order(oid)['mint']))
    assert permitted(store,route,None)
    worker.market_event(dev,'dev-sale',raw['slot']);await worker.once()
    assert not permitted(store,route,None)


@pytest.mark.asyncio
async def test_registration_and_ack_catch_sale_before_subscription(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    worker.history={};worker.subscribed(dev)
    calls=[]
    async def call(method,args):
        calls.append((method,args))
        return [{'signature':'dev-sale','slot':raw['slot'],'err':None}]
    reader.call=call
    await worker.catch_up_once()
    assert len(calls)==1 and dev not in worker.history
    await worker.once();await exit.once()
    assert builds==[1000]
    # A reconnect uses the last fully scanned signature; existing inbox dedupes.
    worker.subscribed(dev);await worker.catch_up_once()
    assert calls[-1][1][1]['until']=='dev-sale'
    await worker.once();await exit.once();assert builds==[1000]


@pytest.mark.asyncio
async def test_history_paginates_to_create_slot_and_ack_during_page(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    worker.history={};worker.subscribed(dev);worker.subscribed(dev)
    calls=[]
    async def call(method,args):
        calls.append(args)
        if len(calls)==1:return [{'signature':f's{i}','slot':raw['slot'],'err':None} for i in range(100)]
        return [{'signature':'older','slot':raw['slot']-2,'err':None}]
    reader.call=call
    await worker.catch_up_once()
    assert worker.history[dev]['before']=='s99' and dev not in worker.history_latest
    await worker.catch_up_once()
    assert worker.history[dev]['until']=='s0'  # ACK requested a final gap-closing pass.
    assert calls[1][1]['before']=='s99'
    assert not store.rows('trading',"SELECT 1 FROM dev_sell_events WHERE signature='older'")
    await worker.catch_up_once();assert dev not in worker.history


@pytest.mark.asyncio
async def test_history_failure_stops_at_ten_minutes_without_blocking_live(config,store):
    worker,exit,reader,oid,dev,raw,builds=await armed(config,store)
    worker.history={};worker.subscribed(dev);worker.history[dev]['first']=time.time()-601
    async def call(method,args):raise TimeoutError()
    reader.call=call
    await worker.catch_up_once();assert dev not in worker.history
    assert worker.notices.items[-1][0][0]=='dev sell catch-up incomplete'
    worker.market_event(dev,'dev-sale',raw['slot']);await worker.once();await exit.once()
    assert builds==[1000]
