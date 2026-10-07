"""Explicit local dotenv; environment overrides file, no parent config imports."""
from dataclasses import dataclass, field
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


def market_cap_micros(value):
    try:
        n=Decimal(str(value))*10**9
        if not n.is_finite() or n!=n.to_integral_value() or not 0<=n<10**24:
            raise ValueError('invalid-market-cap-usd-k')
        return int(n)
    except InvalidOperation as exc:
        raise ValueError('invalid-market-cap-usd-k') from exc


def hours_to_seconds(value, minimum=60):
    try:
        seconds = Decimal(str(value)) * 3600
        if (not seconds.is_finite() or seconds != seconds.to_integral_value()
                or not minimum <= seconds <= 30*86400):
            raise ValueError('invalid-hour-duration')
        return int(seconds)
    except InvalidOperation as exc:
        raise ValueError('invalid-hour-duration') from exc


def percent(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not Decimal('0.01') <= result <= 20:
            raise ValueError('invalid-slippage-percent')
        return result
    except InvalidOperation as exc:
        raise ValueError('invalid-slippage-percent') from exc


def risk_bps(value):
    try:
        n = Decimal(str(value)) * 100
        if not n.is_finite() or not 0 <= n <= 2000 or n != n.to_integral_value():
            raise ValueError('invalid-risk-percent')
        return int(n)
    except InvalidOperation as exc:
        raise ValueError('invalid-risk-percent') from exc


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
    rpc: str = field(repr=False)
    ws: str = field(repr=False)
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
    slippage_percent: Decimal
    min_liquidity: int
    backfill_age: int
    db_max: int
    db_target: int
    min_disk_free: int
    audit_retention: int
    wallet_file: Path | None
    wallet_address: str
    telegram_token: str
    telegram_chat: str
    strategy: str = 'sol-follow-v1'
    alchemy_key: str = field(default='', repr=False)
    feed_mode: str = 'websocket'
    grpc_endpoint: str = 'https://solana-mainnet.streaming.alchemy.com'
    public_timeout: float = 2.0
    history_rps: int = 2
    max_pool_fee_bps: int = 200
    max_total_fee_bps: int = 300
    max_price_impact_bps: int = 200
    quote_cache_ttl_ms: int = 2000
    quote_cache_accounts: int = 512
    hotlist_commitment: str = 'confirmed'
    max_observed_buy: int = 5_000_000_000
    blockhash_cache_ttl_ms: int = 5000
    blockhash_refresh_ms: int = 1000
    max_market_cap_usd_micros: int = 0

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
    key = get('ALCHEMY_API_KEY', '') or get('SOL_ALCHEMY_API_KEY', '')
    if any(c.isspace() for c in key) or any(c in key for c in '/?#'):
        raise ValueError('invalid-alchemy-api-key')
    rpc = get('SOL_RPC_HTTP_URL', 'https://api.mainnet-beta.solana.com')
    ws = get('SOL_RPC_WS_URL', 'wss://api.mainnet-beta.solana.com')
    feed_mode = get('SOL_FEED_MODE', 'auto')
    if feed_mode == 'auto':
        feed_mode = 'alchemy_grpc' if key else 'websocket'
    if feed_mode not in ('alchemy_grpc', 'websocket') or (feed_mode == 'alchemy_grpc' and not key):
        raise ValueError('alchemy-grpc-requires-api-key-or-invalid-feed-mode')
    hotlist_commitment = get('SOL_HOTLIST_COMMITMENT', 'processed' if feed_mode == 'alchemy_grpc' else 'confirmed')
    if hotlist_commitment not in ('processed', 'confirmed') or (hotlist_commitment == 'processed' and feed_mode != 'alchemy_grpc'):
        raise ValueError('processed-hotlist-requires-alchemy-grpc')
    maximum_buy = lamports(get('SOL_FOLLOW_MAX_TARGET_BUY_SOL', '5'))
    if maximum_buy <= 0:
        raise ValueError('invalid-target-buy-limit')
    endpoint = get('SOL_ALCHEMY_GRPC_ENDPOINT', 'https://solana-mainnet.streaming.alchemy.com')
    parsed = urlparse(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('invalid-grpc-endpoint')
    for url, schemes in ((rpc, {'https', 'http'}), (ws, {'wss', 'ws'})):
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
    return Config(
        root=root, data=root/'data', dry_run=dry, rpc=rpc, ws=ws,
        cex=cex_load(root/'cex_addresses.json'), source_programs=programs, privacy_pools=pools,
        min_funding=low, max_funding=high,
        hotlist_ttl=hours_to_seconds(get('SOL_HOTLIST_TTL_HOUR', '24')),
        n=integer('SOL_FOLLOW_MIN_WALLETS', 3, 1, 10000),
        window=integer('SOL_FOLLOW_WINDOW_SECONDS', 120, 1, 86400),
        remaining=boolean(get('SOL_FOLLOW_REQUIRE_REMAINING_POSITION', 'true')),
        signal_age=integer('SOL_SIGNAL_MAX_AGE_SECONDS', 15, 1, 300), buy_amount=amount,
        slippage_percent=percent(get('SOL_SLIPPAGE_PERCENT', '2')),
        min_liquidity=lamports(get('SOL_MIN_POOL_LIQUIDITY_SOL', '10')),
        backfill_age=integer('SOL_BACKFILL_MAX_AGE_SECONDS', 3600, 60, 86400),
        db_max=maximum, db_target=target,
        min_disk_free=integer('SOL_MIN_DISK_FREE_BYTES', 256*1024**2, 1024**2, 1024**4),
        audit_retention=hours_to_seconds(get('SOL_AUDIT_RETENTION_HOUR', '72'), minimum=3600),
        wallet_file=wallet, wallet_address=address,
        telegram_token=get('TELEGRAM_BOT_TOKEN', ''), telegram_chat=get('TELEGRAM_CHAT_ID', ''),
        alchemy_key=key, feed_mode=feed_mode,
        grpc_endpoint=endpoint,
        public_timeout=integer('SOL_PUBLIC_RPC_TIMEOUT_MS', 2000, 100, 8000)/1000,
        history_rps=integer('SOL_BACKGROUND_RPC_RPS', 2, 1, 20),
        max_pool_fee_bps=risk_bps(get('SOL_MAX_POOL_FEE_PERCENT', '2')),
        max_total_fee_bps=risk_bps(get('SOL_MAX_TOTAL_FEE_PERCENT', '3')),
        max_price_impact_bps=risk_bps(get('SOL_MAX_PRICE_IMPACT_PERCENT', '2')),
        quote_cache_ttl_ms=integer('SOL_QUOTE_CACHE_TTL_MS', 2000, 100, 5000),
        quote_cache_accounts=integer('SOL_QUOTE_CACHE_ACCOUNTS', 512, 32, 2048),
        hotlist_commitment=hotlist_commitment, max_observed_buy=maximum_buy,
        max_market_cap_usd_micros=market_cap_micros(get('SOL_FOLLOW_MAX_MARKET_CAP_USD_K','0')),
        blockhash_cache_ttl_ms=integer('SOL_BLOCKHASH_CACHE_TTL_MS',5000,1000,10000),
        blockhash_refresh_ms=integer('SOL_BLOCKHASH_REFRESH_MS',1000,250,1000),
    )
