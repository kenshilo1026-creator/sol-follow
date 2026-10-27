from dataclasses import replace
import pytest
from chain_common.primitives import WSOL
from chain_common.transaction import Tx
from features.strategy.dev_create import create_route
from launchpads.create import decode
from tests.test_create_decoders import pump,launchlab,USDC
from tests.test_quote_cache import manager
from tests.helpers import address


@pytest.mark.parametrize('venue',['pump','stonk'])
@pytest.mark.parametrize('quote',[WSOL,USDC])
def test_create_hint_and_table_scope(venue,quote):
    raw=pump(quote=quote) if venue=='pump' else launchlab(quote=quote)
    table=address()
    raw['transaction']['message']['addressTableLookups']=[{'accountKey':table}]
    tx=Tx(raw);item=decode(tx)[0];route=create_route(item,tx)
    request=route.request();hint=request['createHint']
    assert (hint['mint'],hint['pool'],hint['slot'])==(item.mint,item.pool,item.slot)
    assert request['lookupTables']==([] if quote==WSOL else [table])
    if venue=='stonk':assert hint['config']==tx.top[0]['accounts'][2]
    assert 'createHint' not in replace(route,create_hint=None).request()


def test_stonk_shared_warm_is_nonblocking_deduplicated_and_includes_sol(config,store):
    builder=manager(config,store)
    item=decode(Tx(launchlab()))[0]
    builder.observe_create(item);builder.observe_create(item)
    assert builder.process is None and len(builder.shared_queue)==1
    request=next(iter(builder.shared_queue.values()))
    assert request['createHint']['config']==item.flags['config']
    assert not builder.queue  # SOL does not need a quote conversion warmup.
    builder.shared_seen.add(item.flags['config']);builder.shared_queue.clear()
    builder.observe_create(item)
    assert not builder.shared_queue
