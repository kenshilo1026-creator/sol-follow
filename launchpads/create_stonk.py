"""Verified Stonk LaunchLab initialize ABIs; quote assets are not hardcoded.

Sources: raydium-sdk-V2 launchpad/instrument.ts and pda.ts. Migration metadata
is decoded as opaque fields; there is no migrated-pool execution adapter.
"""
from chain_common.borsh import Reader
from chain_common.primitives import SYSTEM,TOKEN,pda,pubkey,discriminator
from chain_common.transaction import Unsupported
from launchpads import stonk
from launchpads.create_pump import TOKEN_2022,METADATA,RENT
from launchpads.create_types import accounts,signers,expect,event

INITIALIZE=discriminator('global','initialize')
INITIALIZE_V2=discriminator('global','initialize_v2')
INITIALIZE_2022=discriminator('global','initialize_with_token_2022')
DISCRIMINATORS={INITIALIZE,INITIALIZE_V2,INITIALIZE_2022}


def decode(tx,path,ix,data):
    a=accounts(tx,path,ix)
    token2022=data[:8]==INITIALIZE_2022
    v2=data[:8]==INITIALIZE_V2
    base_count=15 if token2022 else 18
    if len(a)<base_count or len(a)>base_count+(2 if token2022 or v2 else 0):
        raise Unsupported('stonk-create-account-count')
    user,creator,config,platform,authority,pool,mint,quote,vault,quote_vault=a[:10]
    if platform!=stonk.PLATFORM:return None
    if mint==quote:raise Unsupported('create-identical-mints')
    signers(tx,path,mint,user)
    expect(authority,pda([b'vault_auth_seed'],stonk.PROGRAM))
    expect(pool,stonk.pool_address(mint,quote))
    for actual,token in ((vault,mint),(quote_vault,quote)):
        expect(actual,pda([b'pool_vault',bytes(pubkey(pool)),bytes(pubkey(token))],stonk.PROGRAM))
    base_program=TOKEN_2022 if token2022 else TOKEN
    quote_program=a[11] if token2022 else a[12]
    if quote_program not in (TOKEN,TOKEN_2022) or (not token2022 and not v2 and quote_program!=TOKEN):
        raise Unsupported('stonk-quote-token-program')
    if token2022:
        expect(a[10],TOKEN_2022)
        expect(a[12:15],[SYSTEM,pda([b'__event_authority'],stonk.PROGRAM),stonk.PROGRAM])
    else:
        expect(a[10],pda([b'metadata',bytes(pubkey(METADATA)),bytes(pubkey(mint))],METADATA))
        expect(a[11],TOKEN)
        expect(a[13:18],[METADATA,SYSTEM,RENT,pda([b'__event_authority'],stonk.PROGRAM),stonk.PROGRAM])
    allow,rule=[pda([seed,bytes(pubkey(platform)),bytes(pubkey(config))],stonk.PROGRAM)
                for seed in (b'platform_allow_config',b'platform_curve_rule')]
    # The SDK permits either optional account independently, or both in order.
    if a[base_count:] not in ([],[allow],[rule],[allow,rule]):
        raise Unsupported('stonk-optional-platform-account')
    r=Reader(data[8:])
    decimals=r.u8()
    name,symbol,uri=r.string(128),r.string(64),r.string(2048)
    kind=r.u8()
    if kind not in (0,1,2):raise Unsupported('stonk-curve-variant')
    supply=r.u64()
    total_sell=r.u64() if kind==0 else None
    fundraising=r.u64()
    migration=r.u8()
    if migration not in (0,1):raise Unsupported('stonk-migration-variant')
    locked,cliff,unlock=r.u64(),r.u64(),r.u64()
    flags={'curve_kind':kind,'supply_raw':str(supply),'total_sell_raw':str(total_sell) if total_sell is not None else None,
           'fundraising_raw':str(fundraising),'migration_type':migration,
           'locked_raw':str(locked),'cliff_period':str(cliff),'unlock_period':str(unlock)}
    if v2 or token2022:
        side=r.u8()
        if side not in (0,1,2):raise Unsupported('stonk-migration-fee-side')
        flags['migration_fee_side']=side
    if token2022:
        extension=r.u8()
        if extension not in (0,1):raise Unsupported('stonk-transfer-fee-option')
        # The SDK emits a zero struct with a false flag; also accept an absent
        # optional struct. A true flag always requires the entire fee payload.
        fee_bps,max_fee=(0,0) if extension==0 and r.remaining==0 else (r.u16(),r.u64())
        if fee_bps>10000:raise Unsupported('stonk-transfer-fee-range')
        flags.update(transfer_fee_enabled=bool(extension),transfer_fee_bps=fee_bps,maximum_fee_raw=str(max_fee))
    r.finish()
    return event(tx,path,launchpad=stonk.NAME,program=stonk.PROGRAM,
                 instruction='initialize_with_token_2022' if token2022 else 'initialize_v2' if v2 else 'initialize',
                 mint=mint,quote_mint=quote,base_token_program=base_program,quote_token_program=quote_program,
                 creator=creator,user=user,pool=pool,name=name,symbol=symbol,uri=uri,decimals=decimals,flags=flags)
