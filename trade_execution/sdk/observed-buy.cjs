 'use strict';
// Replacement cost at the configured SOL cap, computed from local snapshots.
// The caller chooses a cache-only view for decisions; warming runs separately.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const {buildLocal}=require('./local-routes.cjs');
const {safeMint}=require('./build.cjs');
const risk=require('./risk.cjs');
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
module.exports={quoteLimit};
