"""Pure funding decoders: CEX SOL/USDC transfers and Privacy Cash native SOL."""
import struct
from dataclasses import dataclass, asdict
from chain_common.primitives import SYSTEM, TOKEN, USDC, PRIVACY, b58decode, discriminator, pda, pubkey


@dataclass(frozen=True)
class Funding:
    event: str
    signature: str
    wallet: str
    source: str
    provider: str
    amount: int
    slot: int
    time: int
    asset: str = 'SOL'
    wallet_in_keys: bool = True

    def dict(self):
        return asdict(self)


def usdc_candidates(tx, config):
    """Yield (candidate, rejection reason), using only confirmed transaction meta.

    Source ownership plus its signature is required: a CEX signer elsewhere in
    the transaction or a delegated transfer from someone else's account is not
    evidence of a CEX withdrawal. Amounts are raw six-decimal USDC units.
    """
    balances = {}
    for phase in ('pre', 'post'):
        entries = {}
        for row in tx.meta.get(phase+'TokenBalances') or []:
            index = int(row['accountIndex'])
            if not 0 <= index < len(tx.keys) or tx.keys[index] in entries:
                raise ValueError('invalid-funding-token-balances')
            entries[tx.keys[index]] = row
        balances[phase] = entries

    def identity(row):
        return bool(row and row.get('owner') and row.get('mint') == USDC
                    and row.get('programId', TOKEN) == TOKEN
                    and row.get('uiTokenAmount', {}).get('decimals') == 6)

    def amount(row):
        if row is None:
            return 0
        n = int(row['uiTokenAmount']['amount'])
        if not 0 <= n < 2**64:
            raise ValueError('invalid-funding-token-amount')
        return n

    for path, ix in tx.instructions():
        parsed = ix.get('parsed', {})
        if ix.get('programId') != TOKEN or parsed.get('type') not in ('transfer', 'transferChecked'):
            continue
        info = parsed.get('info', {})
        source_account, destination = info.get('source'), info.get('destination')
        before_source = balances['pre'].get(source_account)
        after_destination = balances['post'].get(destination)
        if not identity(before_source) or not identity(after_destination):
            continue
        source, wallet = before_source['owner'], after_destination['owner']
        if source not in config.cex:
            continue
        pubkey(wallet)
        if parsed['type'] == 'transferChecked':
            token_amount = info.get('tokenAmount', {})
            if info.get('mint') != USDC or token_amount.get('decimals') != 6:
                continue
            value = int(token_amount['amount'])
        else:
            value = int(info['amount'])
        item = Funding(f'{tx.signature}:{path}:{wallet}:USDC', tx.signature, wallet, source,
                       'cex:'+config.cex[source], value, tx.slot, tx.time, 'USDC', wallet in tx.keys)
        if source not in tx.signers or info.get('authority') != source:
            reason = 'cex-source-not-signer'
        elif wallet in config.cex or wallet == source:
            reason = 'destination-is-cex'
        elif value < config.min_funding_usdc:
            reason = 'funding-below-minimum'
        elif value > config.max_funding_usdc:
            reason = 'funding-above-maximum'
        else:
            after_source = balances['post'].get(source_account)
            before_destination = balances['pre'].get(destination)
            stable_owners = all(row is None or identity(row) and row['owner'] == owner
                                for row, owner in ((after_source, source), (before_destination, wallet)))
            received = tx.owner_tokens('post', wallet, USDC)-tx.owner_tokens('pre', wallet, USDC)
            proven = (stable_owners and amount(before_source)-amount(after_source) >= value
                      and amount(after_destination)-amount(before_destination) >= value and received >= value)
            reason = '' if proven else 'funding-balance-proof-unavailable'
        yield item, reason


def decode(tx, config):
    found = [item for item, reason in usdc_candidates(tx, config) if not reason]
    for path, ix in tx.instructions():
        if ix.get('programId') != SYSTEM or ix.get('parsed', {}).get('type') != 'transfer':
            continue
        info = ix['parsed']['info']
        source, dest, value = info['source'], info['destination'], int(info['lamports'])
        if source not in config.cex or source not in tx.signers or dest in config.cex or source == dest:
            continue
        if dest in tx.keys and config.min_funding <= value <= config.max_funding and tx.delta(dest) >= value:
            pubkey(dest)
            found.append(Funding(f'{tx.signature}:{path}:{dest}:SOL', tx.signature, dest, source,
                                 'cex:'+config.cex[source], value, tx.slot, tx.time))
    # Whole-transaction balance proof must be unambiguous: only a single
    # native Transact invocation, no arbitrary surrounding program calls.
    privacy_ix = [ix for ix in tx.top if ix.get('programId') == PRIVACY]
    if PRIVACY not in config.source_programs or len(privacy_ix) != 1:
        return found
    if any(ix.get('programId') not in {PRIVACY, 'ComputeBudget111111111111111111111111111111'} for ix in tx.top):
        return found
    ix = privacy_ix[0]
    try:
        raw, a = b58decode(ix['data']), tx.accounts(ix)
        if raw[:8] != discriminator('global', 'transact') or len(a) != 11 or len(raw) > 4096:
            return found
        # Anchor Proof=480 bytes, followed by ExtDataMinified(i64,u64), two Vec<u8>.
        ext, fee = struct.unpack_from('<qQ', raw, 488)
        cursor = 504
        for _ in range(2):
            size, = struct.unpack_from('<I', raw, cursor)
            cursor += 4 + size
            if size > 1024 or cursor > len(raw):
                return found
        if cursor != len(raw) or ext >= 0:
            return found
        pool, dest, fee_dest, payer = a[5], a[7], a[8], a[9]
        if (pool not in config.privacy_pools or pool != pda([b'tree_token'], PRIVACY)
                or a[0] != pda([b'merkle_tree'], PRIVACY)
                or a[6] != pda([b'global_config'], PRIVACY) or a[10] != SYSTEM
                or payer not in tx.signers or len({pool, dest, fee_dest, payer}) != 4
                or not config.min_funding <= -ext <= config.max_funding
                or tx.delta(pool) != ext-fee or tx.delta(dest) != -ext or tx.delta(fee_dest) != fee):
            return found
        found.append(Funding(f'{tx.signature}:privacy:{dest}:SOL', tx.signature, dest, pool,
                             'privacy-cash', -ext, tx.slot, tx.time))
    except (KeyError, ValueError, IndexError, struct.error):
        pass
    return found
