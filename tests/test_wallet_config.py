"""Synthetic wallet secrets stay in Python; no RPC or real wallet is used."""
import json
import traceback

import pytest
from solders.keypair import Keypair

from share_common.config import load
from trade_execution.builder import child_environment, payload_for


def wallet_config(root, secret='', dry=True):
    (root/'cex_addresses.json').write_text('{"exchanges": {}}')
    return load(root=root, env={'SOL_WALLET_ADDRESS': secret, 'DRY_RUN': str(dry).lower()})


@pytest.mark.parametrize('dry', [True, False])
def test_private_key_derives_public_wallet_without_key_file(tmp_path, dry, monkeypatch):
    key = Keypair()
    secret = str(key)
    # Also exercise normal .env loading instead of only environment overrides.
    (tmp_path/'.env').write_text('SOL_WALLET_ADDRESS='+secret+'\n')
    (tmp_path/'cex_addresses.json').write_text('{"exchanges": {}}')
    cfg = load(root=tmp_path, env={'DRY_RUN': str(dry).lower()})
    assert cfg.wallet_address == str(key.pubkey())
    assert cfg.wallet_keypair.sign_message(b'offline-test').verify(key.pubkey(), b'offline-test')
    assert secret not in repr(cfg)
    payload = payload_for(cfg, {'route': 'pump_native_curve'}, cfg.wallet_address)
    assert payload['wallet'] == str(key.pubkey())
    assert secret not in json.dumps(payload)
    monkeypatch.setenv('SOL_WALLET_ADDRESS', secret)
    assert 'SOL_WALLET_ADDRESS' not in child_environment()
    assert secret not in child_environment().values()


def test_empty_wallet_allowed_only_for_dry_run(tmp_path):
    cfg = wallet_config(tmp_path)
    assert cfg.wallet_keypair is None and cfg.wallet_address == ''
    with pytest.raises(ValueError, match='live-requires-SOL_WALLET_ADDRESS-private-key'):
        wallet_config(tmp_path, dry=False)


@pytest.mark.parametrize('secret', [str(Keypair().pubkey()), 'invalid-secret-!@#', '[1,2,3]'])
def test_invalid_private_key_rejected_without_echo(tmp_path, secret):
    with pytest.raises(ValueError, match='invalid-base58-private-key') as caught:
        wallet_config(tmp_path, secret)
    rendered = ''.join(traceback.format_exception(caught.value))
    assert secret not in rendered
