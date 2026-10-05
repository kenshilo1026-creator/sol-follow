"""Pure funding decoders: direct SOL CEX transfers and Privacy Cash native SOL."""
import struct
from dataclasses import dataclass, asdict
from chain_common.primitives import SYSTEM, PRIVACY, b58decode, discriminator, pda, pubkey


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

    def dict(self):
        return asdict(self)


def decode(tx, config):
    found = []
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
