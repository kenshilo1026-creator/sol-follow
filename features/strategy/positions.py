"""Profit-over-entry and current-position semantics; fills complete rules."""
from decimal import Decimal
import hashlib
import json


def load_rules(path):
    doc=json.loads(path.read_text(encoding='utf-8'))
    if doc.get('gain_definition')!='profit_over_entry_percent' or doc.get('sell_basis')!='current_position':
        raise ValueError('unsupported-take-profit-semantics')
    result=[]
    for item in doc['rules']:
        kind=item.get('type','profit')
        if kind not in ('profit','stop_loss','protect','trailing'):
            raise ValueError('unsupported-take-profit-type')
        required={'profit':['profit_percent'],'stop_loss':['loss_percent'],
                  'protect':['activate_profit_percent','profit_percent'],
                  'trailing':['activate_profit_percent','drawdown_percent']}[kind]
        values={k:Decimal(str(item[k])) for k in [*required,'sell_percent']}
        if any(not v.is_finite() or v<0 for v in values.values()) or not 0<values['sell_percent']<=100:
            raise ValueError('invalid-take-profit-value')
        if kind=='trailing' and not 0<values['drawdown_percent']<=100:
            raise ValueError('invalid-trailing-drawdown')
        result.append((hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()[:20],kind,values))
    return sorted(result,key=lambda r:0 if r[1] in ('stop_loss','protect','trailing') else 1)


def select_rule(rules, position, gain, peak):
    done=json.loads(position['rules'])
    for rid,kind,v in rules:
        if rid in done:
            continue
        hit = (kind=='profit' and gain>=v.get('profit_percent',0) or
               kind=='stop_loss' and gain<=-v.get('loss_percent',0) or
               kind=='protect' and peak>=v.get('activate_profit_percent',0) and gain<=v.get('profit_percent',0) or
               kind=='trailing' and peak>=v.get('activate_profit_percent',0) and
               (100+gain)*100 <= (100+peak)*(100-v.get('drawdown_percent',0)))
        if hit:
            return rid,max(1,int(Decimal(position['amount'])*v['sell_percent']/100))
    return None
