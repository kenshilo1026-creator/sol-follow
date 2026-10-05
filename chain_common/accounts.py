"""Shared program-owned account decoding for origin proofs."""
import base64
from chain_common.transaction import Unsupported


def account_data(row, owner, size=None):
    if not row or row['owner']!=owner or row.get('executable'):
        raise Unsupported('account-owner-or-executable')
    data = row['data']
    if not isinstance(data,list) or data[1]!='base64':
        raise Unsupported('base64-account-required')
    raw = base64.b64decode(data[0],validate=True)
    if size and len(raw)!=size:
        raise Unsupported('account-layout-size')
    return raw


def key(raw, offset):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(raw[offset:offset+32]))
