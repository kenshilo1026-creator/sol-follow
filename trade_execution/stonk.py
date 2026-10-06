"""Executed, top-level Stonk LaunchLab buys; funding currency is independent.

Only the proven Stonk platform and its derived pool/vaults are accepted. Net
token balance gains include Token-2022 withholding; gross CPI amounts do not.
"""
from collections import Counter
from dataclasses import dataclass
from chain_common.primitives import TOKEN, TOKEN_2022, SYSTEM, WSOL, ata, b58decode, discriminator, pda, pubkey
from features.strategy.trade import Trade
from launchpads.stonk import PROGRAM, PLATFORM, pool_address

BUY = discriminator('global', 'buy_exact_in')
AUTH = pda([b'vault_auth_seed'], PROGRAM)
EVENT = pda([b'__event_authority'], PROGRAM)


@dataclass(frozen=True)
class StonkRoute:
    trade: Trade
    quote_mint: str
    token_program: str
    quote_program: str
    lookup_tables: tuple
    kind: str = 'sol_to_stonk_curve'
    observed_amount: int = 0
    observed_mint: str = ''
    observed_kind: str = 'exact-input-budget'
    funding_sol_limit: int = 0

    def request(self):
        return dict(route=self.kind, mint=self.trade.mint, quoteMint=self.quote_mint,
                    tokenProgram=self.token_program, quoteProgram=self.quote_program,
                    pool=self.trade.pool, lookupTables=list(self.lookup_tables), minSlot=self.trade.slot)


def decode_stonk(tx):
    candidates = []
    identities = Counter()
    for index, ix in enumerate(tx.top):
        if ix.get('programId') != PROGRAM or not ix.get('data'):
            continue
        raw, a = b58decode(ix['data']), tx.accounts(ix)
        if raw[:8] != BUY or len(raw) != 32 or len(a) not in (18, 19):
            continue
        wallet, mint, quote = a[0], a[9], a[10]
        identities[wallet, mint] += 1
        if (wallet not in tx.signers or a[3] != PLATFORM or a[1] != AUTH
                or a[13] != EVENT or a[14] != PROGRAM
                or a[11] not in (TOKEN, TOKEN_2022) or a[12] not in (TOKEN, TOKEN_2022)
                or a[15] != SYSTEM or quote == SYSTEM or mint == quote
                or (quote == WSOL and a[12] != TOKEN)):
            continue
        if (a[4] != pool_address(mint, quote)
                or a[5] != ata(wallet, mint, a[11]) or a[6] != ata(wallet, quote, a[12])
                or a[7] != pda([b'pool_vault', bytes(pubkey(a[4])), bytes(pubkey(mint))], PROGRAM)
                or a[8] != pda([b'pool_vault', bytes(pubkey(a[4])), bytes(pubkey(quote))], PROGRAM)):
            continue
        limit, minimum = (int.from_bytes(raw[n:n+8], 'little') for n in (8, 16))
        post = tx.tokens('post').get(a[5])
        pre = tx.tokens('pre').get(a[5], (wallet, mint, 0))
        if not post or post[:2] != (wallet, mint) or pre[:2] != (wallet, mint):
            continue
        paid = received = 0
        for inner in tx.inner.get(index, []):
            parsed = inner.get('parsed', {})
            if inner.get('stackHeight') != 2 or parsed.get('type') != 'transferChecked':
                continue
            info = parsed.get('info', {})
            if (inner.get('programId') == a[12] and info.get('source') == a[6]
                    and info.get('destination') == a[8] and info.get('authority') == wallet
                    and info.get('mint') == quote):
                paid += int(info['tokenAmount']['amount'])
            if (inner.get('programId') == a[11] and info.get('source') == a[7]
                    and info.get('destination') == a[5] and info.get('authority') == AUTH
                    and info.get('mint') == mint):
                received += int(info['tokenAmount']['amount'])
        net = post[2] - pre[2]
        # Do not attribute unrelated transfers as buys. Multiple buys of the same
        # wallet/mint are rejected below because balance deltas cannot split them.
        if not (0 < paid <= limit and 0 < net <= received and net >= minimum):
            continue
        candidates.append((index, wallet, mint, quote, a[11], a[12], a[4], paid, net, post[2], limit))
    tables = tuple(r['accountKey'] for r in tx.raw['transaction']['message'].get('addressTableLookups', []))
    if len(tables) > 8:
        return []
    return [StonkRoute(Trade(f'{tx.signature}:{i}', tx.signature, tx.slot, tx.time,
                            wallet, mint, 'buy', paid, net, remaining, pool), quote, base_program, quote_program, tables,
                       kind='stonk_native_curve' if quote == WSOL else 'sol_to_stonk_curve',
                       observed_amount=limit, observed_mint=quote)
            for i, wallet, mint, quote, base_program, quote_program, pool, paid, net, remaining, limit in candidates
            if identities[wallet, mint] == 1]
