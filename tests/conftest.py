from dataclasses import replace
import json
from pathlib import Path
import pytest
from solders.keypair import Keypair
from share_common.config import load
from features.database.storage import Store


@pytest.fixture(autouse=True)
def isolated_default_config(tmp_path,monkeypatch):
    """Default config unit tests must not read deployment CEX lists or secrets."""
    settings=tmp_path/'default-settings'
    settings.mkdir()
    (settings/'cex_addresses.json').write_text(json.dumps({'exchanges':{'test':[str(Keypair().pubkey())]}}))
    monkeypatch.setattr(load,'__defaults__',(settings,None))


@pytest.fixture
def config(tmp_path):
    # Individual amount-gate tests enable their own minimum explicitly.
    fixture_root=tmp_path/'config'
    fixture_root.mkdir()
    (fixture_root/'cex_addresses.json').write_text(json.dumps({'exchanges':{'test':[str(Keypair().pubkey())]}}))
    return replace(load(fixture_root,env={'SOL_WALLET_ADDRESS':'','DRY_RUN':'true'}),
                   root=Path(__file__).resolve().parents[1],data=tmp_path/'data',min_observed_buy=0,min_observed_buy_usd_micros=0,
                   ignore_observed_buy=0,ignore_observed_buy_usd_micros=0,max_market_cap_usd_micros=0,
                   max_dev_holding_tokens=None)


@pytest.fixture
def store(config):
    instance=Store(config.data,reuse_connections=True)
    try:
        yield instance
    finally:
        instance.close()


def address():
    return str(Keypair().pubkey())
