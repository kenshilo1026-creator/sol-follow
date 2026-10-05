"""Venue-independent storage fills and take-profit rule semantics."""
from decimal import Decimal
import json
from features.strategy.positions import select_rule,load_rules
from tests.helpers import address


def test_fill_idempotent_and_remaining_tp(config,store):
    mint,venue=address(),address();oid=store.reserve(config,mint,venue,100)
    assert store.fill(oid,1000,100)
    assert not store.fill(oid,1000,100)
    sold=store.reserve(config,mint,venue,70,'sell','first-tp')
    store.fill(sold,70,28)
    pos=store.rows('trading','SELECT * FROM positions')[0]
    assert pos['amount']=='930' and pos['entry_quote']=='100' and pos['entry_tokens']=='1000'
    assert json.loads(pos['rules'])=={'first-tp':'done'}
    rules=[('first-tp','profit',{'profit_percent':Decimal(300),'sell_percent':Decimal(7)}),
           ('second-tp','profit',{'profit_percent':Decimal(600),'sell_percent':Decimal(7)})]
    assert select_rule(rules,pos,Decimal(650),Decimal(650))==('second-tp',65)


def test_protect_and_trailing(config):
    rules=load_rules(config.root/'take_profit_rules.json')
    position={'amount':'1000','rules':'{}'}
    rid,amount=select_rule(rules,position,Decimal(-7),Decimal(0))
    assert amount==1000
    rid,amount=select_rule(rules,position,Decimal(19),Decimal(60))
    assert amount==1000
