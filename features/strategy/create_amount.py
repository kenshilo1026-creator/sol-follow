"""Dev launch token exposure from calldata and the already received transaction."""
from chain_common.primitives import TOKEN, ata, b58decode, discriminator, pubkey
from launchpads import pump_fun, stonk
from trade_execution.pump_quote import invocations


class AmountUnavailable(ValueError):
    pass


BUY = {discriminator('global',name):name for name in (
    'buy','buy_v2','buy_exact_sol_in','buy_exact_quote_in_v2','buy_exact_in','buy_exact_out')}


def integer(value):
    if (not isinstance(value,str) or not value.isascii() or not value.isdecimal()
            or len(value)>20 or int(value)>2**64-1):
        raise AmountUnavailable('dev-token-amount-invalid')
    return int(value)


def requests(item,tx):
    result=[]
    for path,ix,children,depth in invocations(tx):
        if ix.get('programId')!=item.program or not ix.get('data'):continue
        data=b58decode(ix['data']);name=BUY.get(data[:8])
        if not name:continue
        accounts=tx.accounts(ix)
        if item.mint not in accounts or item.user not in accounts:continue
        target=ata(item.user,item.mint,item.base_token_program)
        if item.launchpad==pump_fun.NAME and len(accounts)==18 and name in ('buy','buy_exact_sol_in'):
            matches=(accounts[2]==item.mint and accounts[3]==item.pool and accounts[5]==target
                     and accounts[6]==item.user and accounts[8]==item.base_token_program)
            partial=len(data)>25 and data[25]!=0
            valid=len(data) in (24,25,26) and all(x in (0,1) for x in data[24:])
        elif item.launchpad==pump_fun.NAME and len(accounts)==27 and name in ('buy_v2','buy_exact_quote_in_v2'):
            matches=(accounts[1]==item.mint and accounts[2]==item.quote_mint
                     and accounts[3]==item.base_token_program and accounts[4]==item.quote_token_program
                     and accounts[10]==item.pool and accounts[13]==item.user and accounts[14]==target)
            partial=len(data)>24 and data[24]!=0
            valid=len(data) in (24,25) and all(x in (0,1) for x in data[24:])
        elif item.launchpad==stonk.NAME and len(accounts) in (18,19) and name in ('buy_exact_in','buy_exact_out'):
            matches=(accounts[0]==item.user and accounts[4]==item.pool and accounts[5]==target
                     and accounts[9]==item.mint and accounts[10]==item.quote_mint
                     and accounts[11]==item.base_token_program and accounts[12]==item.quote_token_program)
            partial=False;valid=len(data)==32
        else:raise AmountUnavailable('dev-buy-calldata-unsupported')
        if not matches or not valid:raise AmountUnavailable('dev-buy-calldata-mismatch')
        exact=name in ('buy','buy_v2','buy_exact_out') and not partial
        result.append({'method':name,'path':path,'destination':target,
            'requested_raw':int.from_bytes(data[8:16],'little') if exact else None})
    return result


def receipt_amount(item,tx,buys):
    """Fresh-mint balance plus outward transfers/burns = net tokens received.

    This does not mistake a later sell/transfer for a smaller dev buy. Metadata
    must be complete enough to identify the new mint and every dev outflow.
    No account/history/mint RPC is performed.
    """
    if tx.signature!=item.signature or tx.slot!=item.slot or tx.meta.get('err') is not None:
        raise AmountUnavailable('dev-create-transaction-mismatch')
    inner=tx.meta.get('innerInstructions')
    if not isinstance(inner,list):raise AmountUnavailable('dev-token-trace-unavailable')
    groups=set()
    for group in inner:
        if (not isinstance(group,dict) or type(group.get('index')) is not int
                or group['index'] in groups or not isinstance(group.get('instructions'),list)):
            raise AmountUnavailable('dev-token-trace-invalid')
        groups.add(group['index'])
    for buy in buys:
        if not tx.inner.get(int(buy['path'].split('.')[0])):
            raise AmountUnavailable('dev-buy-trace-unavailable')
    owners={};post={};target=ata(item.user,item.mint,item.base_token_program)
    rows=0
    for phase in ('pre','post'):
        balances=tx.meta.get(phase+'TokenBalances')
        if not isinstance(balances,list):raise AmountUnavailable('dev-token-metadata-unavailable')
        seen=set()
        for row in balances:
            if not isinstance(row,dict):raise AmountUnavailable('dev-token-metadata-invalid')
            if row.get('mint')!=item.mint:continue
            index=row.get('accountIndex');amount=row.get('uiTokenAmount')
            if (type(index) is not int or not 0<=index<len(tx.keys) or index in seen
                    or not isinstance(amount,dict) or type(amount.get('decimals')) is not int
                    or amount['decimals']!=item.decimals or row.get('programId')!=item.base_token_program
                    or not isinstance(row.get('owner'),str) or not row['owner']):
                raise AmountUnavailable('dev-token-metadata-invalid')
            pubkey(row['owner']);seen.add(index)
            raw=integer(amount.get('amount'));account=tx.keys[index]
            if phase=='pre' and raw:raise AmountUnavailable('create-mint-has-existing-balance')
            if account in owners and owners[account]!=row['owner']:
                raise AmountUnavailable('dev-token-owner-changed')
            owners[account]=row['owner']
            if phase=='post':post[account]=raw;rows+=1
    if not rows:raise AmountUnavailable('dev-token-metadata-unavailable')
    # ATA derivation also identifies an account closed after receiving tokens.
    if target in owners and owners[target]!=item.user:
        raise AmountUnavailable('dev-token-owner-mismatch')
    owners[target]=item.user
    instructions=list(tx.instructions())
    for path,ix in instructions:
        parsed=ix.get('parsed')or{};info=parsed.get('info')or{}
        if (ix.get('programId')==item.base_token_program
                and parsed.get('type') in ('initializeAccount','initializeAccount2','initializeAccount3')
                and info.get('mint')==item.mint):
            account,owner=info.get('account'),info.get('owner')
            if account not in tx.keys or not isinstance(owner,str):
                raise AmountUnavailable('dev-token-account-proof-invalid')
            pubkey(owner)
            if account in owners and owners[account]!=owner:
                raise AmountUnavailable('dev-token-owner-changed')
            owners[account]=owner
    def self_transfer_loss(raw):
        if item.base_token_program==TOKEN:return 0
        # Fresh Stonk mint fees are in the verified create calldata. Pump's
        # canonical mint has no base-token transfer fee. Unknown fee changes
        # cannot be silently ignored in the balance equation.
        for _,operation in instructions:
            parsed=operation.get('parsed')or{}
            if not isinstance(parsed,dict):continue
            info=parsed.get('info')or{}
            if (operation.get('programId')==item.base_token_program
                    and isinstance(info,dict) and info.get('mint')==item.mint
                    and (parsed.get('type')=='setTransferFee'
                         or parsed.get('type')=='initializeTransferFeeConfig' and item.launchpad!=stonk.NAME)):
                raise AmountUnavailable('dev-transfer-fee-unavailable')
        if item.launchpad!=stonk.NAME or not item.flags.get('transfer_fee_enabled'):return 0
        bps=item.flags.get('transfer_fee_bps');maximum=integer(item.flags.get('maximum_fee_raw'))
        if type(bps) is not int or not 0<=bps<=10000:
            raise AmountUnavailable('dev-transfer-fee-unavailable')
        return min((raw*bps+9999)//10000,maximum)
    outgoing=0;closed=set()
    harmless={'initializeAccount','initializeAccount2','initializeAccount3','initializeMint','initializeMint2',
              'initializeImmutableOwner','initializeMetadataPointer','initializeTokenMetadata',
              'initializeTransferFeeConfig','getAccountDataSize','setAuthority','approve','approveChecked',
              'revoke','freezeAccount','thawAccount','mintTo','mintToChecked','closeAccount'}
    for path,ix in instructions:
        if ix.get('programId')!=item.base_token_program:continue
        parsed=ix.get('parsed');accounts=tx.accounts(ix)
        if isinstance(parsed,dict):
            kind=parsed.get('type');info=parsed.get('info')or{}
            if kind=='closeAccount':closed.add(info.get('account'))
            if kind in ('transfer','transferChecked','transferCheckedWithFee'):
                source,dest=info.get('source'),info.get('destination')
                if info.get('mint') not in (None,item.mint):continue
                if owners.get(source)!=item.user:continue
                raw=integer((info.get('tokenAmount')or{}).get('amount') if kind!='transfer' else info.get('amount'))
                if dest not in owners:raise AmountUnavailable('dev-transfer-owner-unavailable')
                outgoing+=raw if owners[dest]!=item.user else self_transfer_loss(raw)
            elif kind in ('burn','burnChecked'):
                account=info.get('account')
                if info.get('mint') not in (None,item.mint):continue
                if owners.get(account)==item.user:
                    outgoing+=integer((info.get('tokenAmount')or{}).get('amount') if kind=='burnChecked' else info.get('amount'))
            elif kind not in harmless and (info.get('mint')==item.mint or any(owners.get(a)==item.user for a in accounts)):
                raise AmountUnavailable('dev-token-instruction-unsupported')
        elif ix.get('data'):
            data=b58decode(ix['data']);opcode=data[0] if data else -1
            if opcode in (3,12) and len(data) in (9,10):
                source=accounts[0] if accounts else None
                dest=accounts[1] if opcode==3 and len(accounts)>=2 else accounts[2] if len(accounts)>=3 else None
                if owners.get(source)==item.user:
                    if dest not in owners:raise AmountUnavailable('dev-transfer-owner-unavailable')
                    raw=int.from_bytes(data[1:9],'little')
                    outgoing+=raw if owners[dest]!=item.user else self_transfer_loss(raw)
            elif opcode in (8,15) and len(data) in (9,10):
                if accounts and owners.get(accounts[0])==item.user:outgoing+=int.from_bytes(data[1:9],'little')
            elif opcode==9 and accounts:closed.add(accounts[0])
            elif any(owners.get(a)==item.user for a in accounts):
                # An opaque extension/movement must never look like zero tokens.
                raise AmountUnavailable('dev-token-instruction-unsupported')
    for buy in buys:
        if buy['destination'] not in post and buy['destination'] not in closed:
            raise AmountUnavailable('dev-buy-balance-unavailable')
    held=sum(raw for account,raw in post.items() if owners[account]==item.user)
    return held+outgoing,held,outgoing


def analyze(item,tx):
    if item.launchpad==stonk.NAME and integer(item.flags.get('locked_raw','0')):
        raise AmountUnavailable('dev-vesting-quantity-unavailable')
    buys=requests(item,tx)
    received,held,outgoing=receipt_amount(item,tx,buys)
    specified=sum(b['requested_raw'] for b in buys) if buys and all(b['requested_raw'] is not None for b in buys) else None
    # A verified exact-output request can decide the cap locally. Keep actual
    # receipt evidence as a backstop for additional allocations in the same tx.
    # With Stonk transfer taxes, use net receipt units instead of gross request.
    untaxed=item.launchpad==pump_fun.NAME or not item.flags.get('transfer_fee_enabled')
    checked=max(received,specified) if specified is not None and untaxed else received
    source='calldata+transaction-meta' if specified is not None and untaxed else 'transaction-meta'
    return {'policy':'create-transaction-v1','amount_raw':str(checked),'decimals':item.decimals,
            'buy_received_raw':str(received),'post_hold_raw':str(held),'outgoing_raw':str(outgoing),
            'calldata_requested_raw':str(specified) if specified is not None else None,
            'quantity_source':source,'creator':item.user,'metadata_creator':item.creator,
            'snapshot_slot':item.slot,'buy_methods':[b['method'] for b in buys]}
