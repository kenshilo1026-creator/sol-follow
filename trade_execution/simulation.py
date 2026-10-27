"""Reuse the trusted local SDK simulation only for the matching unsigned buy."""
import base64
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

COMPUTE_BUDGET='ComputeBudget111111111111111111111111111111'
MAX_UNITS=1_400_000


def reusable(result, final_tx, *, slot, commitment):
    proof=result.get('simulation')
    if not isinstance(proof,dict):return False
    if (type(proof.get('version')) is not int or proof['version']!=1
        or 'err' not in proof or proof['err'] is not None
        or proof.get('commitment')!=commitment
        or type(proof.get('slot')) is not int or proof['slot']<slot
        or type(proof.get('unitsConsumed')) is not int
        or not 0<proof['unitsConsumed']<=MAX_UNITS):return False
    encoded=proof.get('transaction')
    if not isinstance(encoded,str) or len(encoded)>1644:return False
    try:
        wire=base64.b64decode(encoded,validate=True)
        if len(wire)>1232:return False
        simulated=VersionedTransaction.from_bytes(wire)
        before,after=simulated.message,final_tx.message
        if not isinstance(before,MessageV0) or not isinstance(after,MessageV0):return False
        if any(any(bytes(sig)) for tx in (simulated,final_tx) for sig in tx.signatures):return False
        if (before.header!=after.header or before.account_keys!=after.account_keys
            or before.recent_blockhash!=after.recent_blockhash
            or before.address_table_lookups!=after.address_table_lookups
            or not before.instructions or not after.instructions
            or before.instructions[1:]!=after.instructions[1:]):return False
        limits=[]
        for msg in (before,after):
            ix=msg.instructions[0]
            if (str(msg.account_keys[ix.program_id_index])!=COMPUTE_BUDGET
                or ix.accounts or len(ix.data)!=5 or ix.data[0]!=2):return False
            limits.append(int.from_bytes(ix.data[1:],'little'))
        padded=min(MAX_UNITS,(proof['unitsConsumed']*6+4)//5)
        return limits==[MAX_UNITS,padded] and result.get('units')==padded
    except Exception:
        return False  # A legacy/malformed attestation must never bypass simulation.
