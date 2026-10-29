import base64
import json
from pathlib import Path
import pytest
from chain_common.transaction import Tx
from features.strategy.create_snapshot import capture
from features.strategy.dev_create import create_route
from launchpads.create import decode

FIXTURES=Path(__file__).parent/'fixtures'
SAMPLES=json.loads((FIXTURES/'local_create_inputs.json').read_text())


def source(sample):
    path=FIXTURES/sample['file']
    if not path.exists():path=FIXTURES/'create_amount_live'/sample['file']
    return json.loads(path.read_text())


@pytest.mark.parametrize('sample',SAMPLES,ids=lambda x:x['file'][:18])
def test_fixture_evidence_reproduced_from_full_transactions(sample):
    tx=Tx(source(sample));item=decode(tx)[0]
    assert create_route(item,tx).request()==sample['input']
    assert capture(item,tx)['tradeCount']==sample['input']['createHint']['localSnapshot']['tradeCount']


@pytest.mark.parametrize('change',['truncated','unbalanced','failed-cpi','unknown-action','mint-mutation','missing-logs','wrong-slot','wrong-signature'])
def test_incomplete_or_mutated_source_falls_back(change):
    raw=source(SAMPLES[2]);tx=Tx(raw);item=decode(tx)[0]
    if change=='truncated':raw['meta']['logMessages'].append('Log truncated')
    elif change=='unbalanced':raw['meta']['logMessages'].pop()
    elif change=='failed-cpi':raw['meta']['logMessages'].append(f'Program {item.program} failed: custom program error: 1')
    elif change=='unknown-action':raw['meta']['logMessages'] += [f'Program {item.program} invoke [1]',
        'Program log: Instruction: SetCreator',f'Program {item.program} success']
    elif change=='mint-mutation':raw['meta']['logMessages'] += [f'Program {item.base_token_program} invoke [1]',
        'Program log: Instruction: SetAuthority',f'Program {item.base_token_program} success']
    elif change=='missing-logs':raw['meta'].pop('logMessages')
    elif change=='wrong-slot':tx.slot+=1
    elif change=='wrong-signature':tx.signature='wrong'
    assert capture(item,tx) is None


def test_foreign_program_cannot_spoof_launchpad_event():
    raw=source(SAMPLES[2]);tx=Tx(raw);item=decode(tx)[0]
    expected=capture(item,tx)
    fake=base64.b64encode(b'forged event').decode()
    raw['meta']['logMessages'] += ['Program 11111111111111111111111111111111 invoke [1]',
        'Program data: '+fake,'Program 11111111111111111111111111111111 success']
    assert capture(item,tx)==expected


def test_feature_switch_default_and_explicit_disable(config):
    from share_common.config import load
    from trade_execution.builder import payload_for
    assert config.local_create_build
    assert payload_for(config,{},'wallet')['localCreateEnabled'] is True
    assert load(env={'DRY_RUN':'true','SOL_LOCAL_CREATE_BUILD':'false'}).local_create_build is False
