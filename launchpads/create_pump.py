"""Pump create/create_v2 ABI, including explicit non-SOL quote accounts.

Source: pump-fun/pump-public-docs/idl/pump.json. This decodes creation only.
"""
from chain_common.borsh import Reader
from chain_common.primitives import SYSTEM,TOKEN,ATA,WSOL,pda,pubkey,discriminator
from chain_common.transaction import Unsupported
from launchpads import pump_fun
from launchpads.create_types import accounts,signers,expect,event

TOKEN_2022='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
METADATA='metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s'
RENT='SysvarRent111111111111111111111111111111111'
MAYHEM='MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e'
CREATE=discriminator('global','create')
CREATE_V2=discriminator('global','create_v2')
DISCRIMINATORS={CREATE,CREATE_V2}


def associated(owner,mint,program):
    return pda([bytes(pubkey(owner)),bytes(pubkey(program)),bytes(pubkey(mint))],ATA)


def decode(tx,path,ix,data):
    a=accounts(tx,path,ix)
    v2=data[:8]==CREATE_V2
    if (v2 and len(a) not in (16,19,20)) or (not v2 and len(a)!=14):
        raise Unsupported('pump-create-account-count')
    mint,authority,curve,vault,global_account=a[:5]
    expect(authority,pda([b'mint-authority'],pump_fun.PROGRAM))
    expect(curve,pump_fun.curve_address(mint))
    expect(global_account,pda([b'global'],pump_fun.PROGRAM))
    user=a[5] if v2 else a[7]
    base_program=TOKEN_2022 if v2 else TOKEN
    signers(tx,path,mint,user)
    expect(vault,associated(curve,mint,base_program))
    if v2:
        expect(a[6:10],[SYSTEM,TOKEN_2022,ATA,MAYHEM])
        expect(a[10],pda([b'global-params'],MAYHEM))
        expect(a[11],pda([b'sol-vault'],MAYHEM))
        expect(a[12],pda([b'mayhem-state',bytes(pubkey(mint))],MAYHEM))
        # The IDL does not publish a PDA seed constraint for mayhem_token_vault.
        # Do not assume it is the mayhem-state ATA; the program validates it.
        expect(a[14:16],[pda([b'__event_authority'],pump_fun.PROGRAM),pump_fun.PROGRAM])
    else:
        expect(a[5],METADATA)
        expect(a[6],pda([b'metadata',bytes(pubkey(METADATA)),bytes(pubkey(mint))],METADATA))
        expect(a[8:14],[SYSTEM,TOKEN,ATA,RENT,pda([b'__event_authority'],pump_fun.PROGRAM),pump_fun.PROGRAM])
    r=Reader(data[8:])
    name,symbol,uri=r.string(128),r.string(64),r.string(2048)
    creator=r.public_key()
    flags={}
    if v2:
        flags['mayhem_mode']=r.boolean()
        # Pump OptionBool/OptionU64 are transparent, EOF-tolerant wrappers,
        # not Borsh Option tags. Reject partial numbers and unrecognized tails.
        if r.remaining not in (0,1,2,9,10):raise Unsupported('pump-create-optional-tail')
        flags['cashback_enabled']=r.boolean() if r.remaining else False
        if r.remaining==1:
            # The supplied non-SOL mainnet create has a one-byte suffix where
            # the current published IDL expects an optional u64 first. Preserve
            # that observed ABI difference without guessing the byte's meaning.
            flags.update(creator_fee_bps=None,holder_reward=None,uninterpreted_tail_hex=r.take(1).hex())
        else:
            flags['creator_fee_bps']=r.u64() if r.remaining>=8 else 0
            flags['holder_reward']=r.boolean() if r.remaining else False
    r.finish()
    quote,quote_program=WSOL,TOKEN
    if v2 and len(a)>16:
        quote,quote_vault,quote_program=a[16:19]
        if quote_program not in (TOKEN,TOKEN_2022):raise Unsupported('pump-quote-token-program')
        expect(quote_vault,associated(curve,quote,quote_program))
        if len(a)==20:expect(a[19],pda([b'quote-control'],pump_fun.PROGRAM))
    if quote==mint:raise Unsupported('create-identical-mints')
    return event(tx,path,launchpad=pump_fun.NAME,program=pump_fun.PROGRAM,
                 instruction='create_v2' if v2 else 'create',mint=mint,quote_mint=quote,
                 base_token_program=base_program,quote_token_program=quote_program,
                 creator=creator,user=user,pool=curve,name=name,symbol=symbol,uri=uri,decimals=6,flags=flags)
