'use strict';
// Discover/cache SOL -> quote independently of the observed Pump buyer's funding.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk'),BN=require('bn.js');
const jupiter=require('./jupiter.cjs'),risk=require('./risk.cjs');
async function buildPumpQuote(input,{connection,safeMint,finish,minimums,integer,fraction,exactQuoteInstruction}){
  const user=new web3.PublicKey(input.wallet),mint=new web3.PublicKey(input.mint),quote=new web3.PublicKey(input.quoteMint);
  const baseProgram=new web3.PublicKey(input.tokenProgram),quoteProgram=new web3.PublicKey(input.quoteProgram);
  const curveKey=pump.bondingCurvePda(mint);
  if(quote.equals(spl.NATIVE_MINT)||quote.equals(web3.PublicKey.default)||mint.equals(quote))throw Error('non-native-only');
  if(input.pool&&!curveKey.equals(new web3.PublicKey(input.pool)))throw Error('curve-graduated-or-quote-mismatch');
  for(const p of [baseProgram,quoteProgram])if(![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(x=>x.equals(p)))throw Error('token-program');
  const amount=integer(input.amount),{numerator,denominator}=fraction(input.slippagePercent);
  const slippageBps=numerator.muln(10000).div(denominator.muln(2)).toNumber();
  const targetAta=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram);
  const quoteAta=spl.getAssociatedTokenAddressSync(quote,user,false,quoteProgram);
  const nativeAta=spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user);
  const rows=await connection.getMultipleAccountsInfo([mint,quote,curveKey,pump.GLOBAL_PDA,pump.PUMP_FEE_CONFIG_PDA,targetAta,nativeAta]);
  [baseProgram,quoteProgram,pump.PUMP_PROGRAM_ID,pump.PUMP_PROGRAM_ID,pump.PUMP_FEE_PROGRAM_ID].forEach((p,i)=>{
    if(!rows[i]||rows[i].executable||!rows[i].owner.equals(p))throw Error('account-owner');
  });
  const base=spl.unpackMint(mint,rows[0],baseProgram),quoteInfo=spl.unpackMint(quote,rows[1],quoteProgram);
  safeMint(base);safeMint(quoteInfo);
  const curve=pump.PUMP_SDK.decodeBondingCurve(rows[2]);
  if(curve.complete||!curve.quoteMint.equals(quote))throw Error('curve-graduated-or-quote-mismatch');
  const global=pump.PUMP_SDK.decodeGlobal(rows[3]),feeConfig=pump.PUMP_SDK.decodeFeeConfig(rows[4]);
  const hop=await jupiter.buildHop({user,quoteMint:quote,quoteAta,amount,slippageBps,connection,safeMint,
    minLiquidity:input.minLiquidity,swapRecipe:input.swapRecipe,onRecipe:input.onRecipe,
    waitForBackground:input.waitForBackground,background:input.background});
  const pumpQuote=q=>pump.getBuyTokenAmountFromSolAmount({global,feeConfig,mintSupply:new BN(base.supply.toString()),bondingCurve:curve,amount:new BN(q.toString())});
  const expected=pumpQuote(hop.expected),quoteIn=new BN(hop.minimum.toString());
  const pf=pump.computeFeesBps({global,feeConfig,mintSupply:new BN(base.supply.toString()),
    virtualQuoteReserves:curve.virtualQuoteReserves,virtualTokenReserves:curve.virtualTokenReserves,
    quoteMint:curve.quoteMint,creatorFeeBps:curve.creatorFeeBps});
  const bps=BigInt(pf.protocolFeeBps.add(curve.creator.equals(web3.PublicKey.default)?new BN(0):pf.creatorFeeBps).toString());
  const effective=(hop.expected-1n)*10000n/(10000n+bps);
  const ideal=effective*BigInt(curve.virtualTokenReserves.toString())/BigInt(curve.virtualQuoteReserves.toString());
  const metrics=risk.check(input,[...hop.fees,risk.ppm(hop.expected-effective,hop.expected)],
    [...hop.impacts,risk.impact(expected.toString(),ideal)],[...hop.fees,bps*100n]);
  const {minOut}=minimums(new BN(hop.expected.toString()),expected,input.slippagePercent);
  if(pumpQuote(hop.minimum).lt(minOut))throw Error('rounding-exhausts-slippage');
  const ixs=[spl.createAssociatedTokenAccountIdempotentInstruction(user,nativeAta,user,spl.NATIVE_MINT),
    spl.createAssociatedTokenAccountIdempotentInstruction(user,quoteAta,user,quote,quoteProgram),
    web3.SystemProgram.transfer({fromPubkey:user,toPubkey:nativeAta,lamports:BigInt(amount.toString())}),
    spl.createSyncNativeInstruction(nativeAta),...hop.setup,...hop.swaps];
  const pumpIxs=await pump.PUMP_SDK.buyV2Instructions({global,bondingCurveAccountInfo:rows[2],bondingCurve:curve,
    associatedUserAccountInfo:rows[5],mint,user,amount:minOut,quoteAmount:quoteIn,slippage:0,
    tokenProgram:baseProgram,quoteTokenProgram:quoteProgram});
  ixs.push(...pumpIxs.map(ix=>ix.programId.equals(pump.PUMP_PROGRAM_ID)?exactQuoteInstruction(ix,quoteIn,minOut):ix));
  if(!rows[6])ixs.push(spl.createCloseAccountInstruction(nativeAta,user,user));
  const before=rows[5]?spl.unpackAccount(targetAta,rows[5],baseProgram).amount:0n;
  const touched=[...new Set([quoteAta.toBase58(),...hop.swaps.flatMap(ix=>ix.keys).filter(k=>k.isWritable).map(k=>k.pubkey.toBase58())])];
  if(touched.length>96)throw Error('jupiter-route-rejected');
  const accounts=await connection.getMultipleAccountsInfo(touched.map(k=>new web3.PublicKey(k)));
  const protectedAccounts=[];
  accounts.forEach((row,i)=>{
    if(!row||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(row.owner)))return;
    const address=new web3.PublicKey(touched[i]),value=spl.unpackAccount(address,row,row.owner);
    if(value.owner.equals(user)&&!address.equals(targetAta))protectedAccounts.push({address,program:row.owner,value});
  });
  const check={addresses:[targetAta.toBase58(),...protectedAccounts.map(a=>a.address.toBase58())],verify(values){
    const decode=(address,row,program)=>{
      if(!row)throw Error('pump-fill-rejected');
      return spl.unpackAccount(address,{...row,owner:new web3.PublicKey(row.owner),data:Buffer.from(row.data[0],'base64')},program);
    };
    const post=decode(targetAta,values?.[0],baseProgram);
    if(!post.owner.equals(user)||!post.mint.equals(mint)||post.amount-before<BigInt(minOut.toString()))throw Error('pump-fill-rejected');
    protectedAccounts.forEach((prior,i)=>{
      const post=decode(prior.address,values?.[i+1],prior.program);
      if(!post.owner.equals(user)||!post.mint.equals(prior.value.mint)||post.amount<prior.value.amount
        ||post.delegate?.toBase58()!==prior.value.delegate?.toBase58()||post.delegatedAmount!==prior.value.delegatedAmount
        ||post.closeAuthority?.toBase58()!==prior.value.closeAuthority?.toBase58())throw Error('pump-fill-rejected');
    });
  }};
  const tables=[...new Set([...input.lookupTables,...hop.tables])];
  if(tables.length>8)throw Error('invalid-tables');
  return {...await finish(connection,{...input,lookupTables:tables},user,ixs,check),wallet:user.toBase58(),mint:mint.toBase58(),
    targetAta:targetAta.toBase58(),amount:amount.toString(),quotedOut:expected.toString(),minOut:minOut.toString(),
    quoteIn:quoteIn.toString(),quoteOut:hop.expected.toString(),swapRecipe:hop.swapRecipe,risk:metrics};
}
module.exports={buildPumpQuote};
