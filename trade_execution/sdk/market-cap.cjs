"use strict";
// Spot price * current mint supply, expressed in USD; never a liquidation quote.
const crypto=require('node:crypto'),web3=require('@solana/web3.js'),spl=require('@solana/spl-token');
const pump=require('@pump-fun/pump-sdk'),orca=require('@orca-so/whirlpools-sdk'),dm=require('@meteora-ag/dlmm');
const Decimal=require('decimal.js').clone({precision:80});
const {poolState}=require('./stonk.cjs');
const {validateRecipe}=require('./local-routes.cjs');
const pk=x=>new web3.PublicKey(x),D=x=>new Decimal(x.toString());
const FEED=Buffer.from('ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d','hex');
const deployments=[
  ['pyt2F414BA6dPttK6RddPZUdHfapoBN24GL5wbrPCou','rec2HHDDnjLfj4kE7VyEtFA1HPGQLK33259532cRyHp'],
  ['pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT','rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ'],
];
const ORACLES=deployments.map(([push,receiver])=>({
  address:web3.PublicKey.findProgramAddressSync([Buffer.alloc(2),FEED],pk(push))[0],owner:pk(receiver)}));
const PYTH_DISC=crypto.createHash('sha256').update('account:PriceUpdateV2').digest().subarray(0,8);
const DLMM_PROGRAM=pk('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo');
function usdPrice(row,owner,now){
  const d=row?.data;
  // Borsh PriceUpdateV2: authority, Full verification variant, PriceFeedMessage.
  if(!row||row.executable||!row.owner.equals(owner)||!d||d.length<133||!d.subarray(0,8).equals(PYTH_DISC)
     ||d[40]!==1||!d.subarray(41,73).equals(FEED))throw Error('market-cap-price-unavailable');
  const price=d.readBigInt64LE(73),confidence=d.readBigUInt64LE(81),expo=d.readInt32LE(89),published=d.readBigInt64LE(93);
  if(price<=0n||confidence*100n>price||expo < -18||expo>0||published<=0n
     ||BigInt(now)-published>90n||published>BigInt(now+5))throw Error('market-cap-price-unavailable');
  return {value:D(price).mul(D(10).pow(expo)),published:Number(published)};
}
function spot(step,row,connection){
  const input=pk(step.inputMint),output=pk(step.outputMint),pool=pk(step.pool);
  if(!row||row.executable)throw Error('market-cap-price-unavailable');
  let a,b,ratio;
  if(step.label==='Whirlpool'){
    if(!row.owner.equals(orca.ORCA_WHIRLPOOL_PROGRAM_ID))throw Error('account-owner');
    const state=orca.ParsableWhirlpool.parse(pool,row);
    if(!state||state.liquidity.isZero())throw Error('market-cap-price-unavailable');
    a=state.tokenMintA;b=state.tokenMintB;ratio=D(state.sqrtPrice).pow(2).div(D(2).pow(128));
  }else if(step.label==='Meteora DLMM'){
    if(!row.owner.equals(DLMM_PROGRAM))throw Error('account-owner');
    const state=dm.createProgram(connection).coder.accounts.decode('lbPair',row.data);
    if(!state.binStep||state.status!==0)throw Error('market-cap-price-unavailable');
    a=state.tokenXMint;b=state.tokenYMint;ratio=D(1).add(D(state.binStep).div(10000)).pow(state.activeId);
  }else throw Error('cached-route-rejected');
  if(a.equals(input)&&b.equals(output))return ratio;
  if(b.equals(input)&&a.equals(output))return D(1).div(ratio);
  throw Error('cached-route-rejected');
}
async function marketCap(input,connection,now=Math.floor(Date.now()/1000)){
  const mint=pk(input.mint),quote=pk(input.quoteMint),program=pk(input.tokenProgram);
  const native=quote.equals(spl.NATIVE_MINT);
  if(![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(program)))throw Error('token-program');
  const isPump=['pump_native_curve','sol_to_pump_curve','meteora_dlmm_to_pump_curve'].includes(input.route);
  const isStonk=['stonk_native_curve','sol_to_stonk_curve'].includes(input.route);
  if(!isPump&&!isStonk)throw Error('market-cap-route-unavailable');
  const curve=isPump?pump.bondingCurvePda(mint):pk(input.pool);
  const recipe=native?null:validateRecipe(input.swapRecipe||(input.route==='meteora_dlmm_to_pump_curve'?{
    version:1,tables:[],steps:[{label:'Meteora DLMM',pool:input.pool,inputMint:spl.NATIVE_MINT.toBase58(),outputMint:input.quoteMint}]
  }:null),quote);
  const steps=recipe?.steps||[],keys=[mint,curve,...ORACLES.map(x=>x.address),...steps.map(s=>pk(s.pool))];
  const snapshot=await connection.getMultipleAccountsInfoAndContext(keys),rows=snapshot.value;
  if(!rows[0]||rows[0].executable||!rows[0].owner.equals(program))throw Error('account-owner');
  const supply=spl.unpackMint(mint,rows[0],program);
  if(!supply.isInitialized||supply.supply<=0n)throw Error('market-cap-supply-unavailable');
  let quotePerBase;
  if(isPump){
    if(!rows[1]||rows[1].executable||!rows[1].owner.equals(pump.PUMP_PROGRAM_ID))throw Error('account-owner');
    const state=pump.PUMP_SDK.decodeBondingCurve(rows[1]);
    if(state.complete||!(state.quoteMint.equals(quote)||(native&&state.quoteMint.equals(web3.PublicKey.default))))
      throw Error('curve-graduated-or-quote-mismatch');
    quotePerBase=D(state.virtualQuoteReserves).div(D(state.virtualTokenReserves));
  }else{
    const state=poolState(rows[1],curve,mint,quote);
    quotePerBase=D(state.virtualB+state.realB).div(D(state.virtualA-state.realA));
  }
  let oracle;
  for(let i=0;i<ORACLES.length;i++){
    try{const value=usdPrice(rows[2+i],ORACLES[i].owner,now);
      if(!oracle||value.published>oracle.published)oracle=value;
    }catch{}
  }
  if(!oracle)throw Error('market-cap-price-unavailable');
  let quotePerLamport=D(1);
  steps.forEach((step,i)=>{quotePerLamport=quotePerLamport.mul(spot(step,rows[2+ORACLES.length+i],connection));});
  const lamports=D(supply.supply).mul(quotePerBase).div(quotePerLamport);
  const usd=lamports.div(1e9).mul(oracle.value);
  if(!quotePerBase.isFinite()||!quotePerBase.gt(0)||!quotePerLamport.isFinite()||!quotePerLamport.gt(0)
     ||!usd.isFinite()||!usd.gt(0))throw Error('market-cap-price-unavailable');
  return {marketCapUsdMicros:usd.mul(1e6).ceil().toFixed(0),marketCapUsd:usd.toFixed(6),
    supplyRaw:supply.supply.toString(),quoteMint:input.quoteMint,solUsd:oracle.value.toFixed(8),
    oraclePublished:oracle.published,snapshotSlot:snapshot.context.slot,source:'pool-spot-times-supply'};
}
module.exports={marketCap,usdPrice,spot,ORACLES,FEED,PYTH_DISC};
