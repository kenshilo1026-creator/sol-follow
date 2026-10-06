"""No RPC amount gate. Calldata budgets are upper bounds, never tx SOL deltas."""
from chain_common.primitives import WSOL
from trade_execution.builder import BuildError


async def check(config, route, builder):
    amount, mint = route.observed_amount, route.observed_mint
    detail = {'wallet': route.trade.wallet, 'mint': route.trade.mint,
              'input_mint': mint, 'input_raw': str(amount),
              'max_sol_lamports': str(config.max_observed_buy), 'amount_kind': route.observed_kind}
    if amount <= 0 or not mint:
        return False, {**detail, 'reason': 'buy-budget-unavailable'}
    if route.funding_sol_limit > config.max_observed_buy:
        return False, {**detail, 'reason': 'source-sol-budget-over-limit'}
    if mint == WSOL:
        return amount <= config.max_observed_buy, {**detail, 'sol_lamports': str(amount),
            'reason': 'buy-budget-allowed' if amount <= config.max_observed_buy else 'source-sol-budget-over-limit'}
    try:
        # At the configured SOL cap, how much quote can the cached route buy?
        # This includes first-leg fees/impact instead of scaling a tiny quote
        # linearly. No fresh lookup, quote API, account or price RPC is allowed.
        result = await builder.quote_limit(route)
        quote_limit = int(result['quoteLimit'])
        if quote_limit <= 0:
            raise BuildError('price-cache-miss')
    except Exception as exc:
        reason = str(exc) if isinstance(exc, BuildError) else 'price-cache-unavailable'
        return False, {**detail, 'reason': reason}
    return amount <= quote_limit, {**detail, 'quote_limit_raw': str(quote_limit),
        'reason': 'buy-budget-allowed' if amount <= quote_limit else 'source-token-budget-over-sol-limit'}
