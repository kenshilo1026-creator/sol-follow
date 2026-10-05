"""Pump.fun origin proof from its canonical, program-owned bonding curve."""
from chain_common.primitives import discriminator, pda, pubkey
from chain_common.accounts import account_data
from chain_common.transaction import Unsupported

NAME = 'pump.fun'
PROGRAM = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SWAP_PROGRAM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
EXAMPLE_MINT = 'CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump'


def curve_address(mint):
    return pda([b'bonding-curve', bytes(pubkey(mint))], PROGRAM)


def verify(row):
    if row is None:
        return False
    try:
        raw = account_data(row, PROGRAM)
    except Unsupported:
        return False
    # The original fixed prefix remains present after curve completion/migration.
    return len(raw) >= 49 and raw[:8] == discriminator('account', 'BondingCurve')
