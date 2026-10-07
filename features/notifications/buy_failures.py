"""Public trade context only: never forward provider bodies, URLs or key errors."""
import re
from chain_common.primitives import WSOL, SYSTEM, b58decode
from chain_common.rpc import RpcError
from trade_execution.builder import BuildError

INTERNAL_REASONS = frozenset({
    'position-already-exists', 'signal-expired', 'signal-expired-after-build',
    'invalid-built-transaction', 'simulation-rejected', 'signal-expired-after-simulation',
    'wallet-keypair-mismatch', 'wallet-private-key-required', 'signal-expired-before-signing', 'signal-invalid-before-send',
    'send-signature-mismatch', 'fill-transaction-mismatch',
})


def reason(exc):
    text = str(exc)
    if isinstance(exc, BuildError) and re.fullmatch(r'[a-z]+(?:-[a-z]+)*', text) and len(text) <= 80:
        return text
    if isinstance(exc, ValueError) and text in INTERNAL_REASONS:
        return text
    if isinstance(exc, RpcError) and re.fullmatch(
            r'endpoint-cooling|HTTP-[0-9]{3}|RPC-missing-result|[a-zA-Z]+:(?:RPC--?[0-9]+|[a-zA-Z]+Error)', text):
        return text
    return type(exc).__name__


def context(config, route):
    return {'mode':config.mode,'mint':route.trade.mint,'quote_mint':route.quote_mint,
            'route':route.kind,'source_wallet':route.trade.wallet,
            'source_signature':route.trade.signature,'buy_lamports':str(config.buy_amount)}


def unrecognized_non_sol(tx, routes):
    """Identify skipped known buy instructions, without asserting a valid fill."""
    from launchpads import pump_fun, stonk
    from trade_execution.route import BUY as PUMP_BUY
    from trade_execution.stonk import BUY as STONK_BUY
    handled = {r.trade.event for r in routes}
    for path, ix in tx.instructions():
        event = f'{tx.signature}:{path}'
        if event in handled or not ix.get('data'):
            continue
        try:
            data = b58decode(ix['data'])
            a = tx.accounts(ix)
            if ix.get('programId') == pump_fun.PROGRAM and data[:8] in PUMP_BUY:
                wallet, mint, quote = a[13], a[1], a[2]
            elif ix.get('programId') == stonk.PROGRAM and data[:8] == STONK_BUY:
                wallet, mint, quote = a[0], a[9], a[10]
            else:
                continue
            if quote in (WSOL, SYSTEM) or wallet not in tx.signers:
                continue
            yield {'event':event,'mint':mint,'quote_mint':quote,'source_wallet':wallet,
                   'source_signature':tx.signature,'stage':'decode','reason':'unsupported-or-unverified-buy-route'}
        except (ValueError, IndexError, KeyError, TypeError):
            continue
