"""Per-instruction native SOL buys, including multiple buyers in one transaction."""
from collections import Counter
from chain_common.primitives import TOKEN, TOKEN_2022, SYSTEM, WSOL, ata, b58decode, discriminator, pda
from features.strategy.trade import Trade
from launchpads.pump_fun import PROGRAM, curve_address
from trade_execution.route import Route

BUY=discriminator('global','buy')
EXACT=discriminator('global','buy_exact_sol_in')


def decode_native(tx):
    candidates=[]
    for index,ix in enumerate(tx.top):
        if ix.get('programId')!=PROGRAM or 'data' not in ix:
            continue
        raw=b58decode(ix['data']);a=tx.accounts(ix)
        if raw[:8] not in (BUY,EXACT) or len(raw)!=25 or raw[24] not in (0,1) or len(a)!=18:
            continue
        mint,curve,target,wallet,program=a[2],a[3],a[5],a[6],a[8]
        if wallet not in tx.signers or program not in (TOKEN,TOKEN_2022) or curve!=curve_address(mint):
            continue
        if (a[0]!=pda([b'global'],PROGRAM) or a[7]!=SYSTEM or a[11]!=PROGRAM
            or a[4]!=ata(curve,mint,program) or target!=ata(wallet,mint,program)):
            continue
        first=int.from_bytes(raw[8:16],'little');second=int.from_bytes(raw[16:24],'little')
        if min(first,second)<=0:
            continue
        payments=[];received=0;to_curve=0
        # Attribute only direct CPIs under THIS buy, excluding transaction fees,
        # ATA rent and payments from the other signers in the same signature.
        for inner in tx.inner.get(index,[]):
            if inner.get('stackHeight')!=2:
                continue
            parsed=inner.get('parsed',{});info=parsed.get('info',{})
            if inner.get('programId')==SYSTEM and parsed.get('type')=='transfer' and info.get('source')==wallet:
                if info.get('destination') in {curve,a[1],a[9],a[17]}:
                    amount=int(info['lamports']);payments.append(amount)
                    if info['destination']==curve:to_curve+=amount
            if inner.get('programId')==program and parsed.get('type')=='transferChecked':
                if (info.get('source')==a[4] and info.get('destination')==target
                    and info.get('mint')==mint and info.get('authority')==curve):
                    received+=int(info['tokenAmount']['amount'])
        spent=sum(payments)
        post=tx.tokens('post').get(target)
        if to_curve<=0 or spent<=0 or received<=0 or not post or post[:2]!=(wallet,mint):
            continue
        if raw[:8]==BUY and (received!=first or spent>second):
            continue
        if raw[:8]==EXACT and (spent>first or received<second):
            continue
        # A transfer can change the end balance; retain it separately for the
        # existing 'remaining position' gate instead of counting it as a buy.
        candidates.append((index,mint,wallet,program,curve,spent,received,post[2],first if raw[:8]==EXACT else second,
                           'exact-input-budget' if raw[:8]==EXACT else 'maximum-input-budget'))
    counts=Counter((r[1],r[2]) for r in candidates)
    tables=tuple(row['accountKey'] for row in tx.raw['transaction']['message'].get('addressTableLookups',[]))
    if len(tables)>8:
        return []
    return [Route(Trade(f'{tx.signature}:{i}',tx.signature,tx.slot,tx.time,wallet,mint,'buy',spent,received,remaining,curve),
                  WSOL,program,TOKEN,'',tables,'pump_native_curve',observed_amount=budget,observed_mint=WSOL,observed_kind=kind)
            for i,mint,wallet,program,curve,spent,received,remaining,budget,kind in candidates
            if counts[mint,wallet]==1]
