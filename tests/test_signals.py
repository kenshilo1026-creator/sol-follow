from dataclasses import replace
import time
from features.funding.decoder import Funding
from features.strategy.signals import Signals
from features.strategy.trade import Trade
from tests.helpers import address


def fund(store,wallet,now,slot=1):
    item=Funding('fund:'+wallet,'fund:'+wallet,wallet,address(),'cex:test',10**9,slot,int(now)-10)
    store.admit(item,3600,now)


def trade(wallet,mint,sig,now,slot=2,side='buy',remaining=100):
    return Trade(sig,sig,slot,int(now),wallet,mint,side,10**7,100,remaining,address())


def test_distinct_and_replay(config,store):
    now=time.time();wallets=[address() for _ in range(3)];mint=address()
    engine=Signals(store,config,started=now-1)
    for w in wallets:fund(store,w,now)
    for i in range(10):
        assert engine.observe(trade(wallets[0],mint,str(i),now,slot=i+2),now) is None
    assert engine.observe(trade(wallets[1],mint,'second',now,slot=20),now) is None
    order=engine.observe(trade(wallets[2],mint,'third',now,slot=21),now)
    assert order
    assert engine.observe(trade(wallets[2],mint,'third',now,slot=21),now) is None
    assert engine.observe(trade(wallets[0],mint,'fourth',now,slot=22),now) is None
    assert len(store.rows('trading','SELECT * FROM orders'))==1


def test_sell_revokes_vote(config,store):
    now=time.time();a,b,c=[address() for _ in range(3)];mint=address()
    engine=Signals(store,config,started=now-1)
    for w in (a,b,c):fund(store,w,now)
    engine.observe(trade(a,mint,'a',now),now)
    engine.observe(trade(b,mint,'b',now),now)
    engine.observe(trade(a,mint,'sold',now,slot=3,side='sell',remaining=0),now)
    assert engine.observe(trade(c,mint,'c',now),now) is None
    assert engine.observe(trade(a,mint,'bought-again',now,slot=4),now)


def test_partial_sale_keeps_original_time(config,store):
    now=time.time();a=address();mint=address();fund(store,a,now)
    engine=Signals(store,config,started=now-1)
    engine.observe(trade(a,mint,'buy',now),now)
    engine.observe(trade(a,mint,'sell',now+20,slot=3,side='sell',remaining=10),now+20)
    row=store.rows('trading','SELECT * FROM votes')[0]
    assert row['time']==int(now) and row['amount']=='10'


def test_startup_old_history_no_buy(config,store):
    now=time.time();engine=Signals(store,replace(config,n=1),started=now)
    w,mint=address(),address();fund(store,w,now)
    assert engine.observe(trade(w,mint,'old',now-2),now) is None
    assert engine.observe(trade(w,mint,'new',now+2,slot=3),now+2)


def test_late_funding_not_retroactive(config,store):
    now=time.time();engine=Signals(store,replace(config,n=1),started=now-1)
    w,m=address(),address();fund(store,w,now,slot=5)
    assert engine.observe(trade(w,m,'before',now,slot=4),now) is None
    assert engine.observe(trade(w,m,'same',now,slot=5),now) is None


def test_mode_independent_and_failure_release(config,store):
    mint,pool=address(),address()
    first=store.reserve(config,mint,pool,1)
    assert store.reserve(config,mint,pool,1) is None
    assert store.reserve(replace(config,dry_run=False),mint,pool,1)
    store.fail_order(first,'not-submitted')
    assert store.reserve(config,mint,pool,1)


def test_same_slot_uncertain_revoke(config,store):
    now=time.time();w,m=address(),address();fund(store,w,now)
    e=Signals(store,config,started=now-1)
    e.observe(trade(w,m,'first',now),now)
    e.observe(trade(w,m,'second',now),now)
    assert store.rows('trading','SELECT amount FROM votes')[0]['amount']=='0'
