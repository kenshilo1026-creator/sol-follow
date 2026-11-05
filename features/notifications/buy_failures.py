"""Public trade context only: never forward provider bodies, URLs or key errors."""
import re
import math
from decimal import Decimal
from chain_common.primitives import WSOL, SYSTEM, b58decode
from chain_common.rpc import RpcError
from trade_execution.builder import BuildError

INTERNAL_REASONS = frozenset({
    'token-social-link-missing','token-metadata-unavailable','token-metadata-timeout',
    'dev-entry-not-permitted','source-proof-unusable','source-proof-not-pending',
    'signal-time-in-future','order-source-unusable','dev-sell-monitor-unavailable',
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


def fee_context(exc,config):
    limits={'quote-token-tax-limit':('max_quote_token_tax_bps','SOL_MAX_QUOTE_TOKEN_TAX_PERCENT'),
            'token-tax-limit':('max_token_tax_bps','SOL_MAX_TOKEN_TAX_PERCENT')}
    selected=limits.get(reason(exc))
    if selected is None:return {}
    attr,parameter=selected
    detail=exc.fee_details if isinstance(exc,BuildError) else {}
    def percent(raw):
        value=format(Decimal(raw)/10000,'f')
        return value.rstrip('0').rstrip('.') if '.' in value else value
    return {'fee_parameter':parameter,
            'fee_limit_percent':percent(detail.get('limitPpm',str(getattr(config,attr)*100)))+'%',
            'fee_actual_percent':percent(detail['actualPpm'])+'%' if detail else '未取得'}


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


def build_timeout_lines(detail):
    """Describe recorded timeout progress, without inventing a simulation result."""
    if detail.get('stage') != 'build' or detail.get('reason') not in ('signal-expired', 'route-builder-timeout'):
        return []
    diagnostic = detail.get('builder_diagnostics')
    if not isinstance(diagnostic, dict):
        return []
    labels = {'simulateTransaction': '模擬（simulateTransaction）',
              'getMultipleAccountsInfo': '帳戶讀取（getMultipleAccountsInfo）',
              'getLatestBlockhash': 'Blockhash 讀取（getLatestBlockhash）',
              'localCreateShared': '共用設定準備（localCreateShared）'}
    pending = {}
    progress = diagnostic.get('progress')
    for event in progress[-32:] if isinstance(progress, list) else ():
        if not isinstance(event, dict):
            continue
        stage, phase = event.get('stage'), event.get('phase')
        if not isinstance(stage, str) or stage not in labels:
            continue
        if phase == 'start':
            pending.pop(stage, None)
            pending[stage] = True
        elif phase == 'end':
            pending.pop(stage, None)
    lines = []
    if pending:
        stage = next(reversed(pending))
        lines.append(f'逾時位置: {labels[stage]}已開始，尚未記錄完成')
        if stage == 'simulateTransaction':
            lines.append('模擬結果: 未取得；不能據此判定曲線已完成')
    else:
        phases = {'worker-start': '等待組單 worker 啟動',
                  'worker-write': '傳送組單請求', 'worker-response': '等待組單 worker 回覆'}
        phase = diagnostic.get('phase')
        if isinstance(phase, str) and phase in phases:
            lines.append(f'逾時位置: {phases[phase]}')
    for key, label, factor in (('timeout_s', '本次組單等待上限', 1000),
                                ('elapsed_ms', '本次組單等待耗時', 1)):
        value = diagnostic.get(key)
        if type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value):
            lines.append(f'{label}: {value * factor:.1f} ms')
    return lines
