"""Replay actual compiled mainnet instructions through the Yellowstone adapter.

Fixture: finalized getTransaction(encoding=json), retrieved 2026-10-26 from
api.mainnet-beta.solana.com; signature 5D9F7zeCZJTLKshh5ya7fC6YM2qjEAF2LrfMBcnui47jHVF8mz89pifa8gncQmJxMKo3BAEcP8sARmvRUuqyLgtE.
Do not substitute parsed instructions: that hid the initialization regression.
"""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from chain_common.primitives import b58decode
from chain_common.transaction import Tx
from chain_common.yellowstone.normalize import normalize
from features.strategy.create_amount import analyze, AmountUnavailable
from features.strategy.create_dev_gate import CreateDevGate, permitted
from features.strategy.dev_create import create_route
from launchpads.create import decode
from tests.helpers import Notices
from tests.test_streaming import wire


FIXTURE = Path(__file__).parent / 'fixtures/create_amount_live/4yak4F4qDbB4NoLMyXPJMoWxSKVne1XhZUzsL9DEGem3.raw.json'


def captured_update():
    raw = json.loads(FIXTURE.read_text())
    msg = raw['transaction']['message']
    header = msg['header']
    static = msg['accountKeys']
    signed = header['numRequiredSignatures']
    keys = [dict(pubkey=key, signer=i < signed, source='transaction',
                 writable=(i < signed-header['numReadonlySignedAccounts'] if i < signed
                           else i < len(static)-header['numReadonlyUnsignedAccounts']))
            for i, key in enumerate(static)]
    for kind in ('writable', 'readonly'):
        keys.extend(dict(pubkey=key, signer=False, source='lookupTable', writable=kind == 'writable')
                    for key in raw['meta']['loadedAddresses'][kind])

    def expand(ix):
        return dict(programId=keys[ix['programIdIndex']]['pubkey'], accounts=ix['accounts'],
                    data=ix['data'], stackHeight=ix.get('stackHeight'))

    msg['accountKeys'] = keys
    msg['instructions'] = [expand(ix) for ix in msg['instructions']]
    for group in raw['meta']['innerInstructions']:
        group['instructions'] = [expand(ix) for ix in group['instructions']]
    return wire(raw).transaction, raw['blockTime']


def dev_initializations(update):
    group = next(g for g in update.transaction.meta.inner_instructions if g.index == 3)
    return group.instructions[2], group.instructions[3]


@pytest.mark.parametrize('limit,allowed', [('60000000', True), ('20000000', False)])
def test_actual_stream_create_quantity_and_entry_gate(config, store, limit, allowed):
    update, stamp = captured_update()
    tx = Tx(normalize(update, stamp))
    item = decode(tx)[0]
    result = analyze(item, tx)
    assert result['amount_raw'] == result['calldata_requested_raw'] == '20784503577184'
    assert result['post_hold_raw'] == '20784503577184'
    assert result['outgoing_raw'] == '0'
    assert result['decimals'] == 6
    cfg = replace(config, max_dev_holding_tokens=Decimal(limit))
    route = create_route(item, tx)
    gate = CreateDevGate(cfg, store, Notices())
    assert gate.start_create(item, tx, route) is allowed
    assert permitted(store, route, cfg.max_dev_holding_tokens) is allowed
    assert gate.row(item.mint)['state'] == ('allowed' if allowed else 'blocked')


@pytest.mark.parametrize('mutation', ['unknown', 'immutable-extra-byte', 'owner-truncated', 'missing-mint'])
def test_opaque_or_malformed_dev_initializations_remain_unavailable(config, store, mutation):
    update, stamp = captured_update()
    immutable, initialize = dev_initializations(update)
    if mutation == 'unknown':
        immutable.data = b'\xff'
    elif mutation == 'immutable-extra-byte':
        immutable.data += b'\x00'
    elif mutation == 'owner-truncated':
        initialize.data = initialize.data[:-1]
    else:
        initialize.accounts = initialize.accounts[:1]
    tx = Tx(normalize(update, stamp))
    item = decode(tx)[0]
    with pytest.raises(AmountUnavailable, match='dev-token-instruction-unsupported'):
        analyze(item, tx)
    cfg = replace(config, max_dev_holding_tokens=Decimal('60000000'))
    route = create_route(item, tx)
    gate = CreateDevGate(cfg, store, Notices())
    assert not gate.start_create(item, tx, route)
    assert gate.row(item.mint)['state'] == 'unavailable'
    assert not permitted(store, route, cfg.max_dev_holding_tokens)


def test_decoded_initialization_owner_must_match_transaction_balances():
    update, stamp = captured_update()
    _, initialize = dev_initializations(update)
    initialize.data = bytes([18]) + b58decode('11111111111111111111111111111111')
    tx = Tx(normalize(update, stamp))
    with pytest.raises(AmountUnavailable, match='dev-token-owner-changed'):
        analyze(decode(tx)[0], tx)
