from dataclasses import replace
import pytest
from share_common.config import load
from trade_execution.builder import payload_for


def test_priority_fee_default_and_payload_for_all_execution_sides(config):
    assert config.priority_fee_lamports==200000
    for side in ('buy','sell','sweep'):
        payload=payload_for(config,{'side':side,'priorityFeeLamports':'999999999'},'wallet')
        assert payload['priorityFeeLamports']=='200000'
    assert payload_for(replace(config,priority_fee_lamports=0),{},'wallet')['priorityFeeLamports']=='0'


@pytest.mark.parametrize('value',['-0.1','NaN','Infinity','0.0000000001'])
def test_priority_fee_invalid_settings_rejected(value):
    with pytest.raises(ValueError):load(env={'DRY_RUN':'true','SOL_PRIORITY_FEE_SOL':value})


def test_priority_fee_explicit_zero_and_total_sol_setting():
    assert load(env={'DRY_RUN':'true','SOL_PRIORITY_FEE_SOL':'0'}).priority_fee_lamports==0
    assert load(env={'DRY_RUN':'true','SOL_PRIORITY_FEE_SOL':'0.0002'}).priority_fee_lamports==200000
