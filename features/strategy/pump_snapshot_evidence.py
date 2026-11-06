"""Receipt corroboration for Pump projections, using transaction metadata only."""
from chain_common.primitives import SYSTEM, WSOL, b58decode, b58encode, discriminator
from launchpads.create_pump import associated


def receipt(item, tx):
    vault = associated(item.pool, item.mint, item.base_token_program)
    tokens = tx.tokens('post')
    base = tokens.get(vault)
    if not base or base[:2] != (item.pool, item.mint):
        return None
    result = {'poolTokens': str(base[2])}
    if item.quote_mint != WSOL:
        quote = tokens.get(associated(item.pool, item.quote_mint, item.quote_token_program))
        if not quote or quote[:2] != (item.pool, item.quote_mint):
            return None
        return {**result, 'quoteTokens': str(quote[2])}
    # Only top-level creation/extension rent transfers are independently identified.
    # CPI-only launches still use the strict published event layouts or RPC fallback.
    rent, created = 0, False
    for i, ix in enumerate(tx.top):
        if ix.get('programId') != item.program:
            continue
        data = b58decode(ix.get('data', ''))
        if data[:8] not in (discriminator('global', 'create'), discriminator('global', 'create_v2'),
                           discriminator('global', 'extend_account')):
            continue
        for child in tx.inner.get(i, []):
            if child.get('programId') != SYSTEM:
                continue
            parsed = child.get('parsed', {})
            if not parsed:
                # Yellowstone keeps System CreateAccount as compiled bytes.
                wire = b58decode(child.get('data', ''))
                accounts = tx.accounts(child)
                if len(wire) == 52 and wire[:4] == bytes(4) and len(accounts) == 2:
                    parsed = {'type': 'createAccount', 'info': {'newAccount': accounts[1],
                        'owner': b58encode(wire[20:52]), 'lamports': int.from_bytes(wire[4:12], 'little')}}
            info = parsed.get('info', {})
            if parsed.get('type') == 'createAccount' and info.get('newAccount') == item.pool:
                if created or info.get('owner') != item.program:
                    return None
                created = True
                rent += int(info['lamports'])
            elif parsed.get('type') == 'transfer' and info.get('destination') == item.pool:
                rent += int(info['lamports'])
    index = tx.keys.index(item.pool)
    if not created or tx.meta['preBalances'][index] != 0:
        return None
    return {**result, 'poolLamports': str(tx.meta['postBalances'][index]), 'rentLamports': str(rent)}
