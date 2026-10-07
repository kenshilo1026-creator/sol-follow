 'use strict';
// Replacement cost at the configured SOL cap, computed from local snapshots.
// The caller chooses a cache-only view for decisions; warming runs separately.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const {buildLocal}=require('./local-routes.cjs');
const {safeMint}=require('./build.cjs');
const risk=require('./risk.cjs');
const Decimal=require('decimal.js').clone({precision:80});
const {selectUsd,ORACLES}=require('./market-cap.cjs');
async function quoteLimit(input,connection){
  if(!/^[1-9][0-9]*$/.test(input.limitAmount))throw Error('invalid-u64');
  const amount=new BN(input.limitAmount);if(amount.bitLength()>64)throw Error('invalid-u64');
  const quoteMint=new web3.PublicKey(input.quoteMint),user=new web3.PublicKey(input.wallet);
  const recipe=input.route==='meteora_dlmm_to_pump_curve'?{version:1,tables:[],steps:[{
    label:'Meteora DLMM',pool:input.pool,inputMint:spl.NATIVE_MINT.toBase58(),outputMint:input.quoteMint,
  }]}:input.swapRecipe;
  if(!recipe)throw Error('price-cache-miss');
  const q=await buildLocal({connection,user,quoteMint,amount,safeMint,
    minLiquidity:input.minLiquidity,slippageBps:0},recipe);
  risk.check(input,q.fees,q.impacts);
  return {quoteLimit:q.minimum.toString(),solLimit:amount.toString()};
}
async function quoteUsd(input,connection,key,roundUp,now){
  if(!/^[1-9][0-9]*$/.test(input[key])||input[key].length>24)throw Error('invalid-u64');
  const rows=await connection.getMultipleAccountsInfo(ORACLES.map(o=>o.address));
  const oracle=selectUsd(rows,now);
  // Round the minimum up and maximum down so lamport rounding cannot widen limits.
  const lamports=new Decimal(input[key]).mul(1000).div(oracle.value);
  const amount=(roundUp?lamports.ceil():lamports.floor()).toFixed(0);
  const result=await quoteLimit({...input,limitAmount:amount},connection);
  return {...result,[key]:input[key],solUsd:oracle.value.toFixed(8),oraclePublished:oracle.published};
}
async function quoteMinimumUsd(input,connection,now=Math.floor(Date.now()/1000)){
  return quoteUsd(input,connection,'minimumUsdMicros',true,now);
}
async function quoteMaximumUsd(input,connection,now=Math.floor(Date.now()/1000)){
  return quoteUsd(input,connection,'maximumUsdMicros',false,now);
}
module.exports={quoteLimit,quoteMinimumUsd,quoteMaximumUsd};
