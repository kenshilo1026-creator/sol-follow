"""Creation evidence is separate from trade events and never triggers an order."""
from dataclasses import dataclass,asdict
from chain_common.primitives import WSOL,pubkey
from chain_common.transaction import Unsupported


@dataclass(frozen=True)
class Create:
    event: str
    signature: str
    slot: int
    time: int
    instruction_path: str
    launchpad: str
    program: str
    instruction: str
    mint: str
    quote_mint: str
    base_token_program: str
    quote_token_program: str
    creator: str
    user: str
    pool: str
    name: str
    symbol: str
    uri: str
    decimals: int
    flags: dict

    @property
    def quote_kind(self):return 'sol' if self.quote_mint==WSOL else 'token'

    def dict(self):return {**asdict(self),'quote_kind':self.quote_kind}


def accounts(tx,path,ix):
    raw=ix.get('accounts')
    if not isinstance(raw,list) or len(raw)>32:
        raise Unsupported('create-account-count')
    for key in raw:
        if type(key)==int:
            if not 0<=key<len(tx.keys):raise Unsupported('create-account-index')
        elif not isinstance(key,str) or key not in tx.keys:
            raise Unsupported('create-account-not-in-message')
    result=tx.accounts(ix)
    for key in result:pubkey(key)
    if ix.get('programId') not in tx.keys:
        raise Unsupported('create-program-not-in-message')
    if '.' in path and (type(ix.get('stackHeight'))!=int or ix['stackHeight']<2):
        raise Unsupported('create-cpi-stack-unavailable')
    return result


def signers(tx,path,mint,user):
    # A successful CPI can use invoke_signed for PDA mint/payer; an outer
    # instruction must have the actual transaction signatures required by its ABI.
    if '.' not in path and not {mint,user}<=tx.signers:
        raise Unsupported('create-signers-missing')


def expect(actual,wanted,reason='create-account-proof'):
    if actual!=wanted:raise Unsupported(reason)


def event(tx,path,**fields):
    return Create(event=f'{tx.signature}:{path}:{fields["mint"]}:create',signature=tx.signature,
                  slot=tx.slot,time=tx.time,instruction_path=path,**fields)
