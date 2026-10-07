'use strict';
// LaunchLab bonding curve only. Layout/math follow the official LaunchLab SDK.
const crypto=require('node:crypto');
const web3=require('@solana/web3.js');
const spl=require('@solana/spl-token');
const BN=require('bn.js');
const jupiter=require('./jupiter.cjs');
const risk=require('./risk.cjs');
const PROGRAM=new web3.PublicKey('LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj');
const PLATFORM=new web3.PublicKey('6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt');
const FEE_WALLET=new web3.PublicKey('5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG');
const disc=(ns,name)=>crypto.createHash('sha256').update(ns+':'+name).digest().subarray(0,8);
const pda=(...seeds)=>web3.PublicKey.findProgramAddressSync(seeds,PROGRAM)[0];
const auth=pda(Buffer.from('vault_auth_seed'));
const key=(data,offset)=>new web3.PublicKey(data.subarray(offset,offset+32));
const u64=(data,offset)=>data.readBigUInt64LE(offset);
function account(row,owner,size,name) {
  if(!row||row.executable||!row.owner.equals(owner)||row.data.length<size
    ||(name&&!row.data.subarray(0,8).equals(disc('account',name))))throw Error('stonk-pool-rejected');
  return row.data;
}
function poolState(row,pool,mint,quote) {
  const d=account(row,PROGRAM,429,'PoolState');
  if(d[17]!==0 || !key(d,173).equals(PLATFORM)||!key(d,205).equals(mint)||!key(d,237).equals(quote)
    ||!pool.equals(pda(Buffer.from('pool'),mint.toBuffer(),quote.toBuffer())))throw Error('stonk-pool-rejected');
  const vaultA=pda(Buffer.from('pool_vault'),pool.toBuffer(),mint.toBuffer());
  const vaultB=pda(Buffer.from('pool_vault'),pool.toBuffer(),quote.toBuffer());
  if(!vaultA.equals(key(d,269))||!vaultB.equals(key(d,301)))throw Error('stonk-pool-rejected');
  return {config:key(d,141),creator:key(d,333),vaultA,vaultB,
    totalSell:u64(d,29),virtualA:u64(d,37),virtualB:u64(d,45),realA:u64(d,53),realB:u64(d,61)};
}
function feeSchedule(configRow,platformRow,quote) {
  const c=account(configRow,PROGRAM,115,'GlobalConfig');
  const p=account(platformRow,PROGRAM,728,'PlatformConfig');
  if(c[16]!==0||!key(c,83).equals(quote)||!key(p,16).equals(FEE_WALLET))throw Error('stonk-curve-rejected');
  const rate=u64(c,27)+u64(p,104)+u64(p,720);
  if(rate>=1000000n)throw Error('stonk-curve-rejected');
  return rate;
}
function quoteDetails(state,amount,rate,mint,epoch) {
  const fee=(amount*rate+999999n)/1000000n;
  const effective=amount-fee, x=state.virtualA-state.realA, y=state.virtualB+state.realB;
  if(effective<=0n||x<=0n||y<=0n)throw Error('stonk-curve-rejected');
  const gross=effective*x/(y+effective);
  // The graduation boundary can change instruction requirements; never buy it
  // with this pre-graduation adapter or silently switch to another venue.
  if(gross<=0n||gross>=state.totalSell-state.realA)throw Error('stonk-curve-rejected');
  const transfer=spl.getTransferFeeConfig(mint);
  const tax=transfer?spl.calculateEpochFee(transfer,BigInt(epoch),gross):0n;
  if(gross<=tax)throw Error('stonk-curve-rejected');
  return {net:gross-tax,fees:[risk.ppm(fee,amount),risk.ppm(tax,gross)],impact:risk.impact(gross,effective*x/y)};
}
function quoteNet(...args){
  return quoteDetails(...args).net;
}
function buyInstruction({user,pool,mint,quote,baseProgram,quoteProgram,state,targetAta,quoteAta,amount,minOut}) {
  const addresses=[user,auth,state.config,PLATFORM,pool,targetAta,quoteAta,state.vaultA,state.vaultB,
    mint,quote,baseProgram,quoteProgram,pda(Buffer.from('__event_authority')),PROGRAM,
    web3.SystemProgram.programId,pda(PLATFORM.toBuffer(),quote.toBuffer()),pda(state.creator.toBuffer(),quote.toBuffer())];
  const writable=new Set([0,4,5,6,7,8,16,17]);
  const data=Buffer.alloc(32);disc('global','buy_exact_in').copy(data);
  data.writeBigUInt64LE(amount,8);data.writeBigUInt64LE(minOut,16); // zero share fee
  return new web3.TransactionInstruction({programId:PROGRAM,data,
    keys:addresses.map((pubkey,i)=>({pubkey,isSigner:i===0,isWritable:writable.has(i)}))});
}
async function buildStonk(input,{connection,safeMint,finish,minimums,integer,fraction}) {
  const user=new web3.PublicKey(input.wallet),mint=new web3.PublicKey(input.mint),quote=new web3.PublicKey(input.quoteMint);
  const pool=new web3.PublicKey(input.pool),baseProgram=new web3.PublicKey(input.tokenProgram),quoteProgram=new web3.PublicKey(input.quoteProgram);
  const native=input.route==='stonk_native_curve';
  if(native&&(!quote.equals(spl.NATIVE_MINT)||!quoteProgram.equals(spl.TOKEN_PROGRAM_ID)))throw Error('native-only');
  if((!native&&quote.equals(spl.NATIVE_MINT))||quote.equals(web3.PublicKey.default)||mint.equals(quote))throw Error('non-native-only');
  for(const p of [baseProgram,quoteProgram])if(![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(x=>x.equals(p)))throw Error('token-program');
  const amount=integer(input.amount),{numerator,denominator}=fraction(input.slippagePercent);
  const slippageBps=numerator.muln(10000).div(denominator.muln(2)).toNumber();
  const targetAta=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram);
  const quoteAta=spl.getAssociatedTokenAddressSync(quote,user,false,quoteProgram);
  const nativeAta=spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user);
  const rows=await connection.getMultipleAccountsInfo([pool,mint,quote,targetAta,nativeAta]);
  const state=poolState(rows[0],pool,mint,quote);
  account(rows[1],baseProgram,82);account(rows[2],quoteProgram,82);
  const base=spl.unpackMint(mint,rows[1],baseProgram),quoteInfo=spl.unpackMint(quote,rows[2],quoteProgram);
  safeMint(base,true);safeMint(quoteInfo); // Quote transfer taxes need a different first-leg adapter.
  const [configs,epoch,hop]=await Promise.all([
    connection.getMultipleAccountsInfo([state.config,PLATFORM]),connection.getEpochInfo(),
    native?{expected:BigInt(amount.toString()),minimum:BigInt(amount.toString()),fees:[],impacts:[],setup:[],swaps:[],tables:[]}:
    jupiter.buildHop({user,quoteMint:quote,quoteAta,amount,slippageBps,connection,safeMint,
      minLiquidity:input.minLiquidity,swapRecipe:input.swapRecipe,onRecipe:input.onRecipe,waitForBackground:input.waitForBackground,background:input.background})]);
  const rate=feeSchedule(configs[0],configs[1],quote);
  risk.checkTokenTax(input,base,epoch.epoch);
  const details=quoteDetails(state,hop.expected,rate,base,epoch.epoch),expected=details.net;
  const metrics=risk.check(input,[...hop.fees,...details.fees],[...hop.impacts,details.impact],[...hop.fees,rate]);
  const {minOut}=minimums(new BN(hop.expected.toString()),new BN(expected.toString()),input.slippagePercent);
  if(quoteNet(state,hop.minimum,rate,base,epoch.epoch)<BigInt(minOut.toString()))throw Error('rounding-exhausts-slippage');
  const ixs=[spl.createAssociatedTokenAccountIdempotentInstruction(user,nativeAta,user,spl.NATIVE_MINT),
    spl.createAssociatedTokenAccountIdempotentInstruction(user,quoteAta,user,quote,quoteProgram),
    spl.createAssociatedTokenAccountIdempotentInstruction(user,targetAta,user,mint,baseProgram),
    web3.SystemProgram.transfer({fromPubkey:user,toPubkey:nativeAta,lamports:BigInt(amount.toString())}),
    spl.createSyncNativeInstruction(nativeAta),...hop.setup,...(hop.swaps||[hop.swap]),
    buyInstruction({user,pool,mint,quote,baseProgram,quoteProgram,state,targetAta,quoteAta,
      amount:hop.minimum,minOut:BigInt(minOut.toString())})];
  if(!rows[4])ixs.push(spl.createCloseAccountInstruction(nativeAta,user,user));
  const before=rows[3]?spl.unpackAccount(targetAta,rows[3],baseProgram).amount:0n;
  // A SOL-funded route must not consume pre-existing quote/intermediate assets.
  const touched=native?[nativeAta.toBase58()]:[...new Set((hop.swaps||[hop.swap]).flatMap(ix=>ix.keys).filter(k=>k.isWritable).map(k=>k.pubkey.toBase58()))];
  if(touched.length>96)throw Error('jupiter-route-rejected');
  const existing=await connection.getMultipleAccountsInfo(touched.map(k=>new web3.PublicKey(k)));
  const protectedAccounts=[];
  for(let i=0;i<touched.length;i++){
    const row=existing[i];
    if(!row||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(row.owner)))continue;
    const address=new web3.PublicKey(touched[i]);
    const value=spl.unpackAccount(address,row,row.owner);
    if(value.owner.equals(user)&&!address.equals(targetAta))protectedAccounts.push({address,program:row.owner,value});
  }
  const check={addresses:[targetAta.toBase58(),...protectedAccounts.map(a=>a.address.toBase58())],verify(values){
    const row=values?.[0];
    if(!row)throw Error('stonk-fill-rejected');
    const post=spl.unpackAccount(targetAta,{...row,owner:new web3.PublicKey(row.owner),data:Buffer.from(row.data[0],'base64')},baseProgram);
    if(!post.owner.equals(user)||!post.mint.equals(mint)||post.amount-before<BigInt(minOut.toString()))throw Error('stonk-fill-rejected');
    for(let i=0;i<protectedAccounts.length;i++){
      const prior=protectedAccounts[i],row=values?.[i+1];
      if(!row)throw Error('stonk-fill-rejected');
      const post=spl.unpackAccount(prior.address,{...row,owner:new web3.PublicKey(row.owner),data:Buffer.from(row.data[0],'base64')},prior.program);
      if(!post.owner.equals(user)||!post.mint.equals(prior.value.mint)||post.amount<prior.value.amount
        ||post.delegate?.toBase58()!==prior.value.delegate?.toBase58()
        ||post.delegatedAmount!==prior.value.delegatedAmount
        ||post.closeAuthority?.toBase58()!==prior.value.closeAuthority?.toBase58())throw Error('stonk-fill-rejected');
    }
  }};
  const tables=[...new Set([...input.lookupTables,...hop.tables])];
  if(tables.length>8)throw Error('invalid-tables');
  return {...await finish(connection,{...input,lookupTables:tables},user,ixs,check),wallet:user.toBase58(),
    mint:mint.toBase58(),targetAta:targetAta.toBase58(),amount:amount.toString(),quotedOut:expected.toString(),
    minOut:minOut.toString(),quoteIn:hop.minimum.toString(),quoteOut:hop.expected.toString(),swapRecipe:hop.swapRecipe,risk:metrics};
}
module.exports={buildStonk,poolState,feeSchedule,quoteNet,quoteDetails,buyInstruction,PROGRAM,PLATFORM};
