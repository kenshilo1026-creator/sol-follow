"""Bounded, program-attributed create evidence; never a persistent account cache."""
import base64
import re
from chain_common.primitives import TOKEN
from launchpads.create_pump import TOKEN_2022

CREATES = {'Create', 'CreateV2', 'Initialize', 'InitializeV2', 'InitializeWithToken2022'}
TRADES = {'Buy', 'BuyV2', 'BuyExactSolIn', 'BuyExactQuoteInV2', 'Sell', 'SellV2',
          'BuyExactIn', 'BuyExactOut', 'SellExactIn', 'SellExactOut'}
TOKEN_SAFE = {'GetAccountDataSize', 'InitializeAccount', 'InitializeAccount2', 'InitializeAccount3',
              'InitializeImmutableOwner', 'Transfer', 'TransferChecked', 'SyncNative', 'CloseAccount'}
TOKEN_CREATE = TOKEN_SAFE | {'InitializeMint', 'InitializeMint2', 'MintTo', 'MintToChecked',
                            'SetAuthority', 'InitializeTransferFeeConfig', 'InitializeMetadataPointer'}
INVOKE = re.compile(r'^Program ([1-9A-HJ-NP-Za-km-z]+) invoke \[(\d+)\]$')
SUCCESS = re.compile(r'^Program ([1-9A-HJ-NP-Za-km-z]+) success$')


def capture(item, tx):
    """Only complete successful invocation logs, with one canonical creation.

    Unknown program actions or mint mutations outside its creation make the
    optimization ineligible. The normal account-reading builder remains usable.
    A foreign program's Program data text is never accepted as launchpad state.
    """
    try:
        return _capture(item, tx)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _capture(item, tx):
    if tx.signature != item.signature or tx.slot != item.slot or tx.meta.get('err') is not None:
        return None
    logs = tx.meta.get('logMessages')
    if not isinstance(logs, list) or not logs or len(logs) > 2500:
        return None
    stack, events = [], []
    creates = trades = total = 0
    for line in logs:
        if not isinstance(line, str) or 'Log truncated' in line:
            return None
        if match := INVOKE.fullmatch(line):
            if int(match[2]) != len(stack) + 1:
                return None
            stack.append([match[1], None])
        elif match := SUCCESS.fullmatch(line):
            if not stack or stack[-1][0] != match[1]:
                return None
            stack.pop()
        elif line.startswith('Program ') and ' failed:' in line:
            return None
        elif line.startswith('Program log: Instruction: '):
            if not stack:
                return None
            name = line.removeprefix('Program log: Instruction: ')
            stack[-1][1] = name
            if stack[-1][0] == item.program:
                if name in CREATES:
                    creates += 1
                elif name in TRADES:
                    trades += 1
                else:
                    return None
            if stack[-1][0] in (TOKEN, TOKEN_2022):
                within_create = any(p == item.program and n in CREATES for p, n in stack[:-1])
                if within_create and item.launchpad=='pump.fun' and name=='InitializeTransferFeeConfig':
                    return None
                if name not in (TOKEN_CREATE if within_create else TOKEN_SAFE):
                    return None
        elif line.startswith('Program data: ') and stack and stack[-1][0] == item.program:
            data = line.removeprefix('Program data: ')
            raw = base64.b64decode(data, validate=True)
            total += len(raw)
            if len(raw) < 8 or len(raw) > 8192 or total > 24576 or len(events) >= 32:
                return None
            events.append(data)
    if stack or creates != 1 or not events:
        return None
    return {'version': 1, 'signature': tx.signature, 'slot': tx.slot, 'mint': item.mint,
            'pool': item.pool, 'quoteMint': item.quote_mint, 'creator': item.creator,
            'user': item.user, 'tokenProgram': item.base_token_program,
            'decimals': item.decimals, 'flags': item.flags, 'tradeCount': trades, 'events': events}
