"""Stonk origin proof, using the LaunchLab platform of the supplied example.

Pool layout/PDA: official raydium-sdk-V2 launchpad/layout.ts and pda.ts.
Platform ID and fee wallet were read from the sample's on-chain launch pool.
Names, tickers, DEX labels and fee-wallet mentions are not origin proofs.
"""
from chain_common.primitives import discriminator, pda, pubkey
from chain_common.transaction import Unsupported
from chain_common.accounts import account_data, key

NAME = 'stonk'
PROGRAM = 'LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj'
PLATFORM = '6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt'
FEE_WALLET = '5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG'
EXAMPLE_MINT = '49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6'


def pool_address(mint, quote):
    return pda([b'pool', bytes(pubkey(mint)), bytes(pubkey(quote))], PROGRAM)


def verify_platform(row):
    try:
        raw = account_data(row, PROGRAM)
    except Unsupported:
        return False
    return (len(raw) >= 80 and raw[:8] == discriminator('account', 'PlatformConfig')
            and key(raw, 16) == FEE_WALLET)


def verify_pool(address, row, mint):
    try:
        raw = account_data(row, PROGRAM, 429)
    except Unsupported:
        return False
    return (raw[:8] == discriminator('account', 'PoolState')
            and key(raw, 173) == PLATFORM and key(raw, 205) == mint
            and address == pool_address(mint, key(raw, 237)))
