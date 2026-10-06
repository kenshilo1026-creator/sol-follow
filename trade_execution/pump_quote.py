"""Pump quote buys independent of the payer's preceding swap or token holdings."""
from collections import Counter
from chain_common.primitives import TOKEN, TOKEN_2022, WSOL, SYSTEM, ATA, ata, b58decode, discriminator, pda
from features.strategy.trade import Trade
from launchpads.pump_fun import PROGRAM, curve_address
from trade_execution.route import Route, BUY

EXACT = discriminator('global', 'buy_exact_quote_in_v2')


def invocations(tx):
    for i, ix in enumerate(tx.top):
        yield str(i), ix, tx.inner.get(i, []), 1
        inner = tx.inner.get(i, [])
        for j, nested in enumerate(inner):
            depth = nested.get('stackHeight')
            if not isinstance(depth, int) or depth < 2:
                continue
            end = j+1
            while end < len(inner) and isinstance(inner[end].get('stackHeight'), int) and inner[end]['stackHeight'] > depth:
                end += 1
            yield f'{i}.{j}', nested, inner[j+1:end], depth


def decode_quote(tx, legacy=None):
    rows, identities = [], Counter()
    pre, post = tx.tokens('pre'), tx.tokens('post')
    for path, ix, children, depth in invocations(tx):
        if ix.get('programId') != PROGRAM or not ix.get('data'):
            continue
        try:
            data, a = b58decode(ix['data']), tx.accounts(ix)
            if data[:8] not in BUY or len(a) < 16:
                continue
            wallet, mint, quote = a[13], a[1], a[2]
            identities[wallet, mint] += 1
            if len(a) != 27 or len(data) != 24:
                continue
            base_program, quote_program = a[3], a[4]
            if (wallet not in tx.signers or quote in (SYSTEM, WSOL) or mint == quote
                    or base_program not in (TOKEN, TOKEN_2022) or quote_program not in (TOKEN, TOKEN_2022)
                    or a[0] != pda([b'global'], PROGRAM) or a[5] != ATA
                    or a[10] != curve_address(mint) or a[24] != SYSTEM or a[26] != PROGRAM
                    or a[11] != ata(a[10], mint, base_program) or a[12] != ata(a[10], quote, quote_program)
                    or a[14] != ata(wallet, mint, base_program) or a[15] != ata(wallet, quote, quote_program)):
                continue
            first, second = (int.from_bytes(data[n:n+8], 'little') for n in (8, 16))
            budget, minimum = (first, second) if data[:8] == EXACT else (second, first)
            target, before = post.get(a[14]), pre.get(a[14], (wallet, mint, 0))
            if not target or target[:2] != (wallet, mint) or before[:2] != (wallet, mint):
                continue
            paid = received = curve_paid = 0
            for child in children:
                parsed = child.get('parsed', {})
                if child.get('stackHeight') != depth+1 or parsed.get('type') != 'transferChecked':
                    continue
                info = parsed.get('info', {})
                if (child.get('programId') == quote_program and info.get('source') == a[15]
                        and info.get('authority') == wallet and info.get('mint') == quote):
                    value = int(info['tokenAmount']['amount'])
                    if value < 0:
                        raise ValueError('negative-transfer')
                    paid += value
                    if info.get('destination') == a[12]:
                        curve_paid += value
                if (child.get('programId') == base_program and info.get('source') == a[11]
                        and info.get('destination') == a[14] and info.get('authority') == a[10]
                        and info.get('mint') == mint):
                    received += int(info['tokenAmount']['amount'])
            net = target[2]-before[2]
            if not (0 < curve_paid <= paid <= budget and 0 < minimum <= net <= received):
                continue
            tables = tuple(r['accountKey'] for r in tx.raw['transaction']['message'].get('addressTableLookups', []))
            if len(tables) > 8:
                continue
            hint = legacy if legacy and legacy.trade.event == f'{tx.signature}:{path}' else None
            rows.append(Route(Trade(f'{tx.signature}:{path}', tx.signature, tx.slot, tx.time,
                wallet, mint, 'buy', paid, net, target[2], a[10]), quote, base_program, quote_program,
                hint.dlmm_pool if hint else '', tables, kind='sol_to_pump_curve',
                observed_amount=budget, observed_mint=quote,
                funding_sol_limit=hint.funding_sol_limit if hint else 0))
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return [r for r in rows if identities[r.trade.wallet, r.trade.mint] == 1]
