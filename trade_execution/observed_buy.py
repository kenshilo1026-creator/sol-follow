"""No RPC amount gate. Calldata budgets are upper bounds, never tx SOL deltas."""
from chain_common.primitives import WSOL
from trade_execution.builder import BuildError


async def check(config, route, builder):
    amount, mint = route.observed_amount, route.observed_mint
    detail = {'wallet': route.trade.wallet, 'mint': route.trade.mint,
              'input_mint': mint, 'input_raw': str(amount),
              ('max_sol_lamports' if mint == WSOL else 'max_usd_micros'):
                  str(config.max_observed_buy if mint == WSOL else config.max_observed_buy_usd_micros),
              'amount_kind': route.observed_kind}
    if amount <= 0 or not mint:
        return False, {**detail, 'reason': 'buy-budget-unavailable'}
    if mint == WSOL:
        return amount <= config.max_observed_buy, {**detail, 'sol_lamports': str(amount),
            'reason': 'buy-budget-allowed' if amount <= config.max_observed_buy else 'source-sol-budget-over-limit'}
    try:
        # Independent USD cap, valued using cached Pyth and conversion quotes.
        # No fresh lookup, quote API, account or price RPC is allowed.
        result = await builder.quote_limit(route)
        quote_limit = int(result['quoteLimit'])
        if quote_limit <= 0 or int(result['maximumUsdMicros'])!=config.max_observed_buy_usd_micros:
            raise BuildError('price-cache-miss')
    except Exception as exc:
        reason = str(exc) if isinstance(exc, BuildError) else 'price-cache-unavailable'
        return False, {**detail, 'reason': reason}
    return amount <= quote_limit, {**detail, 'quote_limit_raw': str(quote_limit),
        'reason': 'buy-budget-allowed' if amount <= quote_limit else 'source-token-budget-over-usd-limit'}


async def check_ignore(config, route, builder):
    return await check_minimum(config, route, builder, ignore=True)


async def check_minimum(config, route, builder, *, ignore=False):
    """Use attributed executed payment, never a potentially loose input cap."""
    amount, mint = route.trade.quote, route.quote_mint
    minimum = (config.ignore_observed_buy if mint == WSOL else config.ignore_observed_buy_usd_micros) if ignore else (
        config.min_observed_buy if mint == WSOL else config.min_observed_buy_usd_micros)
    below_reason = "source-buy-below-ignore" if ignore else "source-buy-below-minimum"
    prefix = "ignore" if ignore else "min"
    detail = {'wallet': route.trade.wallet, 'mint': route.trade.mint,
              'input_mint': mint, 'paid_raw': str(amount),
              (prefix+'_sol_lamports' if mint == WSOL else prefix+'_usd_micros'): str(minimum),
              'amount_kind': 'executed-payment'}
    if not minimum:
        return True, {**detail, 'reason': 'minimum-disabled'}
    if amount <= 0 or not mint:
        return False, {**detail, 'reason': 'buy-payment-unavailable'}
    if mint == WSOL:
        return amount >= minimum, {**detail, 'sol_lamports': str(amount),
            'reason': 'minimum-allowed' if amount >= minimum else below_reason}
    try:
        # USD floor -> SOL budget using cached Pyth price -> cached quote output.
        # This independent USD floor replaces the SOL floor for non-SOL pairs.
        result = await (builder.quote_ignore(route) if ignore else builder.quote_minimum(route))
        threshold = int(result['quoteLimit'])
        if threshold <= 0 or int(result['minimumUsdMicros']) != minimum:
            raise BuildError('price-cache-miss')
    except Exception as exc:
        reason = str(exc) if isinstance(exc, BuildError) else 'price-cache-unavailable'
        return False, {**detail, 'reason': reason}
    return amount >= threshold, {**detail, 'quote_minimum_raw': str(threshold),
        'reason': 'minimum-allowed' if amount >= threshold else below_reason}
