"""Recognize the observed direct DLMM -> Pump v2 route, never copy its bytes."""
from dataclasses import dataclass
from chain_common.primitives import TOKEN, TOKEN_2022, WSOL, SYSTEM, ata, b58decode, discriminator, pubkey
from features.strategy.trade import Trade
from launchpads.pump_fun import PROGRAM, curve_address

DLMM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
BUY = {discriminator('global', n) for n in ('buy_v2', 'buy_exact_quote_in_v2')}
SWAP = discriminator('global', 'swap2')


@dataclass(frozen=True)
class Route:
    trade: Trade
    quote_mint: str
    token_program: str
    quote_program: str
    dlmm_pool: str
    lookup_tables: tuple
    kind: str = 'meteora_dlmm_to_pump_curve'

    def request(self):
        return dict(route=self.kind, mint=self.trade.mint, quoteMint=self.quote_mint,
                    tokenProgram=self.token_program, quoteProgram=self.quote_program,
                    pool=self.dlmm_pool, lookupTables=list(self.lookup_tables), minSlot=self.trade.slot)


def decode(tx):
    # Initial adapter supports the reference's top-level atomic sequence only.
    # Reject multi-buy/aggregator ambiguity instead of inventing attribution.
    pumps = [(i, ix) for i, ix in enumerate(tx.top) if ix.get('programId') == PROGRAM
             and 'data' in ix and b58decode(ix['data'])[:8] in BUY]
    if len(pumps) != 1:
        return None
    i, ix = pumps[0]
    a = tx.accounts(ix)
    data = b58decode(ix['data'])
    if len(a) != 27 or len(data) != 24 or not all(int.from_bytes(data[j:j+8], 'little') > 0 for j in (8,16)):
        return None
    mint, quote, base_program, quote_program, wallet = a[1], a[2], a[3], a[4], a[13]
    if quote in (SYSTEM, WSOL) or wallet not in tx.signers or base_program not in (TOKEN,TOKEN_2022) or quote_program not in (TOKEN,TOKEN_2022):
        return None
    if a[10] != curve_address(mint) or a[14] != ata(wallet,mint,base_program) or a[15] != ata(wallet,quote,quote_program):
        return None
    pre, post = tx.tokens('pre'), tx.tokens('post')
    target = post.get(a[14])
    if not target or target[:2] != (wallet,mint):
        return None
    before = pre.get(a[14], (wallet,mint,0))
    if before[:2] != (wallet,mint) or target[2] <= before[2]:
        return None
    swaps=[]
    for swap in tx.top[:i]:
        if swap.get('programId') != DLMM or 'data' not in swap:
            continue
        b=tx.accounts(swap); raw=b58decode(swap['data'])
        if len(b)<16 or len(raw)<28 or raw[:8]!=SWAP or b[10]!=wallet:
            continue
        if {b[6],b[7]}!={quote,WSOL} or b[4]!=ata(wallet,WSOL) or b[5]!=a[15]:
            continue
        if (b[11],b[12]) != ((quote_program,TOKEN) if b[6]==quote else (TOKEN,quote_program)):
            continue
        amount=int.from_bytes(raw[8:16],'little')
        if amount>0:
            swaps.append((b[0],amount))
    if len(swaps)!=1:
        return None
    pool, amount=swaps[0]
    lookups=tuple(r['accountKey'] for r in tx.raw['transaction']['message'].get('addressTableLookups',[]))
    if len(lookups)>8:
        return None
    for key in (pool,*lookups):
        pubkey(key)
    return Route(Trade(f'{tx.signature}:{i}',tx.signature,tx.slot,tx.time,wallet,mint,'buy',
                       amount,target[2]-before[2],target[2],a[10]),
                 quote,base_program,quote_program,pool,lookups)


def decode_all(tx):
    from trade_execution.native import decode_native
    from trade_execution.stonk import decode_stonk
    routes=decode_native(tx)
    non_native=decode(tx)
    return routes+([non_native] if non_native else [])+decode_stonk(tx)
