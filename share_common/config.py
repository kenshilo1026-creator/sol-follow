"""Explicit local dotenv; environment overrides file, no parent config imports."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import dotenv_values
from chain_common.primitives import pubkey, PRIVACY, pda

ROOT = Path(__file__).resolve().parents[1]


def boolean(v):
    if str(v).lower() not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ValueError('invalid-boolean')
    return str(v).lower() in {'true', '1', 'yes'}


def lamports(v):
    try:
        n = Decimal(str(v)) * 10**9
        if not n.is_finite() or n != n.to_integral_value() or not 0 <= n < 2**64:
            raise ValueError('invalid-sol-amount')
        return int(n)
    except InvalidOperation as exc:
        raise ValueError('invalid-sol-amount') from exc


def cex_load(path):
    groups = json.loads(Path(path).read_text(encoding='utf-8'))['exchanges']
    result = {}
    for exchange, rows in groups.items():
        for addr in rows:
            if addr.startswith('0x'):
                continue
            pubkey(addr)
            if addr in result and result[addr] != exchange:
                raise ValueError('cex-address-has-conflicting-labels')
            result[addr] = exchange
    return result


@dataclass(frozen=True)
class Config:
    root: Path
    data: Path
    dry_run: bool
    rpc: str
    ws: str
    background_rpc: str
    cex: dict
    source_programs: tuple
    privacy_pools: tuple
    min_funding: int
    max_funding: int
    hotlist_ttl: int
    n: int
    window: int
    remaining: bool
    signal_age: int
    buy_amount: int
    slippage: int
    priority_fee: int
    cu_limit: int
    cu_price: int
    min_liquidity: int
    max_impact: int
    max_venue_fee: int
    max_total_fee: int
    backfill_age: int
    rpc_rps: int
    workers: int
    max_subscriptions: int
    db_max: int
    db_target: int
    min_disk_free: int
    audit_retention: int
    wallet_file: Path | None
    wallet_address: str
    telegram_token: str
    telegram_chat: str
    blacklist: tuple = ()
    strategy: str = 'sol-follow-v1'

    @property
    def mode(self):
        return 'dry' if self.dry_run else 'live'


def load(root=ROOT, env=None):
    root = Path(root).resolve()
    values = {**dotenv_values(root/'.env'), **(os.environ if env is None else env)}
    def get(k, default):
        return values.get(k, default)
    def integer(k, default, lo, hi):
        n = int(get(k, default))
        if not lo <= n <= hi:
            raise ValueError(f'{k}: outside {lo}..{hi}')
        return n
    def addresses(k):
        rows = tuple(x.strip() for x in get(k, '').split(',') if x.strip())
        for row in rows:
            pubkey(row)
        return tuple(dict.fromkeys(rows))
    rpc = get('SOL_RPC_HTTP_URL', 'https://api.mainnet-beta.solana.com')
    ws = get('SOL_RPC_WS_URL', 'wss://api.mainnet-beta.solana.com')
    bg = get('SOL_BACKGROUND_RPC_HTTP_URL', '') or rpc
    for url, schemes in ((rpc, {'https', 'http'}), (bg, {'https', 'http'}), (ws, {'wss', 'ws'})):
        if urlparse(url).scheme not in schemes or not urlparse(url).hostname:
            raise ValueError('invalid-rpc-url')
    programs, pools = addresses('SOL_HOTLIST_SOURCE_CONTRACT'), addresses('SOL_PRIVACY_POOL_ADDRESSES')
    if any(a != PRIVACY for a in programs) or any(a != pda([b'tree_token'], PRIVACY) for a in pools):
        raise ValueError('unsupported-source-program-or-pool')
    if bool(programs) != bool(pools):
        raise ValueError('privacy-program-and-pool-required-together')
    amount = lamports(get('SOL_BUY_AMOUNT_SOL', '0.01'))
    low, high = lamports(get('SOL_HOTLIST_MIN_FUNDING_SOL', '0.01')), lamports(get('SOL_HOTLIST_MAX_FUNDING_SOL', '100'))
    if amount <= 0 or high < low:
        raise ValueError('invalid-amount-range')
    cu = integer('SOL_COMPUTE_UNIT_LIMIT', 300000, 50000, 1400000)
    price = integer('SOL_COMPUTE_UNIT_PRICE_MICROLAMPORTS', 10000, 0, 10**9)
    priority = integer('SOL_MAX_PRIORITY_FEE_LAMPORTS', 100000, 0, 10**9)
    if (cu*price+999999)//1000000 > priority:
        raise ValueError('priority-fee-over-limit')
    maximum = integer('SOL_DB_MAX_BYTES', 2*1024**3, 1024**2, 1024**4)
    target = integer('SOL_DB_TARGET_BYTES', 1536*1024**2, 512*1024, maximum-1)
    dry = boolean(get('DRY_RUN', 'true'))
    wallet = get('SOL_WALLET_KEYPAIR_PATH', '')
    wallet = (root/wallet).absolute() if wallet else None
    address = get('SOL_WALLET_ADDRESS', '')
    if address:
        pubkey(address)
    if not dry and (not wallet or not address):
        raise ValueError('live-requires-keypair-path-and-explicit-wallet-address')
    return Config(root, root/'data', dry, rpc, ws, bg, cex_load(root/'cex_addresses.json'), programs, pools,
        low, high, integer('SOL_HOTLIST_TTL_SECONDS', 86400, 60, 30*86400),
        integer('SOL_FOLLOW_MIN_WALLETS', 3, 1, 10000), integer('SOL_FOLLOW_WINDOW_SECONDS', 120, 1, 86400),
        boolean(get('SOL_FOLLOW_REQUIRE_REMAINING_POSITION', 'true')),
        integer('SOL_SIGNAL_MAX_AGE_SECONDS', 15, 1, 300), amount,
        integer('SOL_SLIPPAGE_BPS', 200, 1, 2000), priority, cu, price,
        lamports(get('SOL_MIN_POOL_LIQUIDITY_SOL', '10')), integer('SOL_MAX_PRICE_IMPACT_BPS', 300, 1, 2000),
        integer('SOL_MAX_VENUE_FEE_BPS', 100, 0, 2000), integer('SOL_MAX_TOTAL_FEE_LAMPORTS', 200000, 5000, 10**9),
        integer('SOL_BACKFILL_MAX_AGE_SECONDS', 3600, 60, 86400), integer('SOL_BACKGROUND_RPS', 5, 1, 100),
        integer('SOL_FUNDING_WORKERS', 2, 1, 16), integer('SOL_MAX_WS_SUBSCRIPTIONS', 256, 1, 10000),
        maximum, target, integer('SOL_MIN_DISK_FREE_BYTES', 256*1024**2, 1024**2, 1024**4),
        integer('SOL_AUDIT_RETENTION_SECONDS', 3*86400, 3600, 30*86400), wallet, address,
        get('TELEGRAM_BOT_TOKEN', ''), get('TELEGRAM_CHAT_ID', ''), addresses('SOL_BLOCKED_WALLETS'))
