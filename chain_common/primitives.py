"""Integer-only amounts and canonical Solana public keys."""
import hashlib
from solders.pubkey import Pubkey

SYSTEM = '11111111111111111111111111111111'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
WSOL = 'So11111111111111111111111111111111111111112'
PRIVACY = '9fhQBbumKEFuXtMBDw8AaQyAjCorLGJQiS3skWZdQyQD'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def pubkey(value):
    key = Pubkey.from_string(str(value))
    if str(key) != value:
        raise ValueError('noncanonical-pubkey')
    return key


def b58decode(value):
    if not isinstance(value, str) or len(value) > 20000:
        raise ValueError('invalid-base58')
    n = 0
    for ch in value:
        n = n * 58 + ALPHABET.index(ch)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + n.to_bytes((n.bit_length()+7)//8, 'big')


def b58encode(data):
    n, out = int.from_bytes(data, 'big'), ''
    while n:
        n, rem = divmod(n, 58)
        out = ALPHABET[rem] + out
    return '1' * (len(data)-len(data.lstrip(b'\0'))) + out


def discriminator(namespace, name):
    return hashlib.sha256(f'{namespace}:{name}'.encode()).digest()[:8]


def pda(seeds, program):
    return str(Pubkey.find_program_address(seeds, pubkey(program))[0])


def ata(owner, mint, token_program=TOKEN):
    return pda([bytes(pubkey(owner)), bytes(pubkey(token_program)), bytes(pubkey(mint))], ATA)
