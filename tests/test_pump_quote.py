"""No mint whitelist or required first-leg venue for observed Pump quote buys."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest
from chain_common.primitives import TOKEN, TOKEN_2022, ata, b58encode, discriminator
from chain_common.transaction import Tx
from trade_execution.route import decode_all
from trade_execution.pump_quote import decode_quote
from trade_execution.cache import CachedBuilder
from tests.helpers import address, Notices
from types import SimpleNamespace

FIX=Path(__file__).parent/'fixtures'


def sample():
    return json.loads((FIX/'pump_non_sol_buy.json').read_text())


def direct():
    raw=sample()
    # Remove the preceding swap without shifting the Pump's inner group index.
    raw['transaction']['message']['instructions'][6]={'programId':'11111111111111111111111111111111','parsed':{'type':'fixture'}}
    return raw


def test_direct_quote_buy_does_not_need_dlmm_or_sol_spend():
    r=decode_all(Tx(direct()))[0]
    assert r.kind=='sol_to_pump_curve' and not r.dlmm_pool
    assert r.observed_mint==r.quote_mint and r.observed_amount>0
    assert r.request()['pool']==r.trade.pool


@pytest.mark.parametrize('program',[TOKEN,TOKEN_2022])
def test_arbitrary_quote_mint_and_token_program(program):
    raw=direct();ix=raw['transaction']['message']['instructions'][7];a=ix['accounts']
    old_quote=a[2];new_quote=address();mapping={old_quote:new_quote,a[4]:program}
    for i,owner in [(7,a[6]),(9,a[8]),(12,a[10]),(15,a[13]),(17,a[16]),(21,a[20])]:
        mapping[a[i]]=ata(owner,new_quote,program)
    # Replace only the quote program in the Pump header and quote transfers:
    # base may originally use the same token program.
    a[4]=program
    def rewrite(value):
        if isinstance(value,str):return mapping.get(value,value) if value!=TOKEN_2022 else value
        if isinstance(value,list):return [rewrite(x) for x in value]
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        return value
    raw=rewrite(raw)
    for group in raw['meta']['innerInstructions']:
        for child in group['instructions']:
            if child.get('parsed',{}).get('info',{}).get('mint')==new_quote:child['programId']=program
    rows=decode_quote(Tx(raw))
    assert len(rows)==1 and rows[0].quote_mint==new_quote and rows[0].quote_program==program


def test_quote_cpi_buy_in_real_create_sample_is_decoded():
    raw=json.loads((FIX/'create_4Nr7xPstQ6VT9MF1kKwmkRy3b1mXct9UPsNgs53Cpump.json').read_text())
    rows=decode_quote(Tx(raw))
    assert len(rows)==1 and rows[0].trade.event.endswith(':3.0')
    assert rows[0].trade.quote==2615319


@pytest.mark.parametrize('bad',['budget','minimum','authority','destination','no-payments','depth','duplicate','curve'])
def test_ambiguous_or_unproven_buys_never_vote(bad):
    raw=direct();ix=raw['transaction']['message']['instructions'][7];a=ix['accounts']
    group=next(g for g in raw['meta']['innerInstructions'] if g['index']==7)
    if bad in ('budget','minimum'):
        budget=1 if bad=='budget' else 10**12
        minimum=10**18 if bad=='minimum' else 1
        ix['data']=b58encode(discriminator('global','buy_exact_quote_in_v2')+budget.to_bytes(8,'little')+minimum.to_bytes(8,'little'))
    elif bad=='curve':a[10]=address()
    elif bad=='duplicate':raw['transaction']['message']['instructions'].append(deepcopy(ix))
    else:
        for child in group['instructions']:
            info=child.get('parsed',{}).get('info',{})
            if bad=='depth':child['stackHeight']=3
            elif info.get('mint')==a[2]:
                if bad=='authority':info['authority']=address()
                elif bad=='destination':info['destination']=address()
        if bad=='no-payments':group['instructions']=[]
    assert decode_quote(Tx(raw))==[]


def test_observed_dlmm_route_seeds_reusable_quote_recipe(config,store):
    builder=CachedBuilder(config,store,Notices(),SimpleNamespace(active=0))
    r=decode_all(Tx(sample()))[0]
    builder.observe(r)
    recipe=builder.seen.recipe(r.quote_mint)
    assert recipe['steps'][0]['label']=='Meteora DLMM'
    assert recipe['steps'][0]['outputMint']==r.quote_mint
    other=replace(r,trade=replace(r.trade,mint=address()),dlmm_pool='')
    builder.observe(other)
    assert builder.seen.recipe(other.quote_mint)==recipe

@pytest.mark.asyncio
async def test_direct_quote_hotlist_buy_reaches_executor(config,store,monkeypatch):
    import time
    from features.runtime.service import Service
    from features.funding.decoder import Funding
    raw=direct();raw['blockTime']=int(time.time());route=decode_quote(Tx(raw))[0]
    cfg=replace(config,n=1)
    store.admit(Funding('fund','fund',route.trade.wallet,address(),'cex:test',10**9,
        route.trade.slot-1,raw['blockTime']-10),3600,time.time())
    service=Service(cfg,store);service.notices=Notices();service.signals.started=raw['blockTime']-1
    calls=[]
    class Engine:
        async def buy(self,oid,r):calls.append(r)
    class RPC:
        async def transaction(self,sig):return raw
    async def limit(r):return {'quoteLimit':str(r.observed_amount),'maximumUsdMicros':'500000000'}
    service.executor=Engine();monkeypatch.setattr(service.quote_builder,'quote_limit',limit)
    store.enqueue([route.trade.signature])
    service.store.mark_live(route.trade.signature);await service.process({'signature':route.trade.signature},RPC())
    assert len(calls)==1 and calls[0].kind=='sol_to_pump_curve'
    assert builder_request(service)==route.quote_mint


def builder_request(service):
    return service.quote_builder.seen.recent()[0][0]['quoteMint']
