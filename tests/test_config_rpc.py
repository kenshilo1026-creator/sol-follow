from dataclasses import replace
import asyncio
import json
from decimal import Decimal
import pytest
from chain_common.rpc import Rpc,Priority,RpcError
from share_common.config import load,lamports,cex_load
from share_common.metrics import Metrics
from share_common.instance import Instance


@pytest.mark.parametrize('value',['-1','NaN','Infinity','0.0000000001','999999999999999999999999'])
def test_unsafe_sol_amount_rejected(value):
    with pytest.raises(ValueError):lamports(value)


def test_config_safety():
    assert lamports('0.1')==100000000
    with pytest.raises(ValueError):load(env={'DRY_RUN':'false'})
    with pytest.raises(ValueError):load(env={'DRY_RUN':'maybe'})


def test_token_tax_limit_defaults_and_units(tmp_path):
    from trade_execution.builder import payload_for
    (tmp_path/'cex_addresses.json').write_text('{"exchanges":{}}',encoding='utf-8')
    assert load(root=tmp_path,env={}).max_token_tax_bps==200
    for percent,bps in [('0',0),('2',200),('1.25',125),('20',2000)]:
        cfg=load(root=tmp_path,env={'SOL_MAX_TOKEN_TAX_PERCENT':percent})
        assert cfg.max_token_tax_bps==bps
        assert payload_for(cfg,{},'public-wallet')['risk']['tokenTaxBps']==bps
    for invalid in ['-1','NaN','Infinity','2.001','21','']:
        with pytest.raises(ValueError):load(root=tmp_path,env={'SOL_MAX_TOKEN_TAX_PERCENT':invalid})


def test_hours_and_percent_units():
    cfg=load(env={'SOL_HOTLIST_TTL_HOUR':'24','SOL_AUDIT_RETENTION_HOUR':'72','SOL_SLIPPAGE_PERCENT':'2'})
    assert cfg.hotlist_ttl==86400 and cfg.audit_retention==259200
    assert cfg.slippage_percent==Decimal('2')
    cfg=load(env={'SOL_HOTLIST_TTL_HOUR':'0.5','SOL_AUDIT_RETENTION_HOUR':'1.5','SOL_SLIPPAGE_PERCENT':'0.25'})
    assert cfg.hotlist_ttl==1800 and cfg.audit_retention==5400
    assert cfg.slippage_percent==Decimal('0.25')


@pytest.mark.parametrize('key,value',[
    ('SOL_HOTLIST_TTL_HOUR','0'),('SOL_HOTLIST_TTL_HOUR','0.0001'),
    ('SOL_HOTLIST_TTL_HOUR','NaN'),('SOL_HOTLIST_TTL_HOUR','721'),
    ('SOL_AUDIT_RETENTION_HOUR','0.5'),('SOL_AUDIT_RETENTION_HOUR','Infinity'),
    ('SOL_SLIPPAGE_PERCENT','0'),('SOL_SLIPPAGE_PERCENT','21'),
    ('SOL_SLIPPAGE_PERCENT','NaN'),('SOL_SLIPPAGE_PERCENT','abc'),
])
def test_invalid_units_rejected(key,value):
    with pytest.raises(ValueError):load(env={key:value})


def test_instance_lock(tmp_path):
    first=Instance(tmp_path)
    try:
        with pytest.raises(RuntimeError):Instance(tmp_path)
    finally:first.close()
    second=Instance(tmp_path);second.close()


@pytest.mark.asyncio
async def test_429_cooldown_shared_no_secret():
    class Response:
        status=429
        headers={'Retry-After':'30'}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        def __init__(self):self.count=0
        def post(self,*args,**kwargs):self.count+=1;return Response()
    session=Session();priority=Priority()
    first=Rpc(session,'https://rpc.example/secret-key',priority)
    second=Rpc(session,'https://rpc.example/secret-key',priority)
    with pytest.raises(RpcError,match='HTTP-429'):await first.call('getBalance',[])
    with pytest.raises(RpcError,match='endpoint-cooling'):await second.call('getBalance',[])
    assert session.count==1


def test_metrics_bounded():
    m=Metrics()
    for _ in range(1100):m.add('stage',0.001)
    assert m.snapshot()['stage']=={'n':1000,'p50_ms':1.0,'p95_ms':1.0,'p99_ms':1.0}
