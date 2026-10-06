"""Convert executed Yellowstone transactions to the existing Tx input shape.

Block time comes from block metadata (or a bounded public RPC read), never the
provider's delivery timestamp. Preserve v1 budget config, ALT keys and CPI depth.
"""
import struct
from chain_common.primitives import b58encode, SYSTEM, TOKEN, TOKEN_2022
from chain_common.transaction import Unsupported


def normalize(update, block_time=None):
    info = update.transaction
    if not info.HasField('transaction') or not info.HasField('meta'):
        raise Unsupported('stream-missing-transaction-or-meta')
    tx, meta = info.transaction, info.meta
    msg, header = tx.message, tx.message.header
    if meta.HasField('err'):
        raise Unsupported('stream-failed-transaction')
    if (not tx.signatures or bytes(tx.signatures[0]) != bytes(info.signature)
            or any(len(s) != 64 for s in tx.signatures)):
        raise Unsupported('stream-signature-mismatch')
    static = list(msg.account_keys)
    signed = header.num_required_signatures
    if (signed != len(tx.signatures) or signed > len(static)
            or header.num_readonly_signed_accounts > signed
            or header.num_readonly_unsigned_accounts > len(static)-signed):
        raise Unsupported('stream-invalid-header')
    keys = []
    for i, key in enumerate(static):
        keys.append(dict(pubkey=_key(key), signer=i<signed, source='transaction',
            writable=i < signed-header.num_readonly_signed_accounts if i<signed
            else i < len(static)-header.num_readonly_unsigned_accounts))
    for writable, addresses in ((True,meta.loaded_writable_addresses),(False,meta.loaded_readonly_addresses)):
        keys.extend(dict(pubkey=_key(k),signer=False,writable=writable,source='lookupTable') for k in addresses)
    if len(keys) != len(meta.pre_balances) or len(keys) != len(meta.post_balances):
        raise Unsupported('stream-missing-loaded-addresses')

    def instruction(ix, inner=False):
        indices = [ix.program_id_index, *ix.accounts]
        if any(i >= len(keys) for i in indices):
            raise Unsupported('stream-instruction-index')
        accounts = [keys[i]['pubkey'] for i in ix.accounts]
        program = keys[ix.program_id_index]['pubkey']
        raw = bytes(ix.data)
        result = dict(programId=program,accounts=accounts,data=b58encode(raw),
                      stackHeight=ix.stack_height if inner and ix.HasField('stack_height') else (None if inner else 1))
        if program == SYSTEM and len(raw) == 12 and len(accounts) == 2 and raw[:4] == b'\x02\x00\x00\x00':
            result['parsed'] = {'type':'transfer','info':{'source':accounts[0],
                'destination':accounts[1],'lamports':struct.unpack_from('<Q',raw,4)[0]}}
        if program in (TOKEN, TOKEN_2022) and len(raw) == 10 and raw[0] == 12 and len(accounts) >= 4:
            amount = struct.unpack_from('<Q', raw, 1)[0]
            result['parsed'] = {'type':'transferChecked','info':{
                'source':accounts[0], 'mint':accounts[1], 'destination':accounts[2],
                'authority':accounts[3], 'tokenAmount':{'amount':str(amount),'decimals':raw[9]}}}
        return result

    message = dict(accountKeys=keys, recentBlockhash=_key(msg.recent_blockhash),
                   instructions=[instruction(ix) for ix in msg.instructions],
                   addressTableLookups=[dict(accountKey=_key(x.account_key),
                       writableIndexes=list(x.writable_indexes),readonlyIndexes=list(x.readonly_indexes))
                       for x in msg.address_table_lookups])
    if msg.HasField('config'):
        message['transactionConfig'] = {f.name:int(v) for f,v in msg.config.ListFields()}
    version = 1 if msg.HasField('config') else (0 if msg.versioned else 'legacy')
    inner = []
    for group in meta.inner_instructions:
        if group.index >= len(msg.instructions):
            raise Unsupported('stream-inner-instruction-index')
        inner.append(dict(index=group.index,instructions=[instruction(ix,True) for ix in group.instructions]))

    def balance(row):
        if row.account_index >= len(keys):
            raise Unsupported('stream-token-index')
        return dict(accountIndex=row.account_index,mint=row.mint,owner=row.owner,programId=row.program_id,
                    uiTokenAmount=dict(amount=row.ui_token_amount.amount,decimals=row.ui_token_amount.decimals,
                                       uiAmountString=row.ui_token_amount.ui_amount_string))

    return dict(slot=int(update.slot),blockTime=block_time,version=version,
        transaction=dict(signatures=[b58encode(s) for s in tx.signatures],message=message),
        meta=dict(err=None,fee=int(meta.fee),preBalances=list(meta.pre_balances),postBalances=list(meta.post_balances),
                  innerInstructions=inner,logMessages=list(meta.log_messages),
                  preTokenBalances=[balance(x) for x in meta.pre_token_balances],
                  postTokenBalances=[balance(x) for x in meta.post_token_balances]))


def _key(raw):
    if len(raw) != 32:
        raise Unsupported('stream-invalid-key')
    return b58encode(raw)
