from dataclasses import replace
from pathlib import Path
import pytest
from solders.keypair import Keypair
from share_common.config import load
from features.database.storage import Store


@pytest.fixture
def config(tmp_path):
    return replace(load(env={}),data=tmp_path/'data')


@pytest.fixture
def store(config):
    return Store(config.data)


def address():
    return str(Keypair().pubkey())
