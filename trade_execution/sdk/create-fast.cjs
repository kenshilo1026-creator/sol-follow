'use strict';
// Create-derived addresses are hints, never synthetic mint/curve state.
const web3=require('@solana/web3.js'),pump=require('@pump-fun/pump-sdk');
const {PLATFORM,PROGRAM}=require('./stonk.cjs');
function hint(input){
  const h=input.createHint;
  if(!h||h.version!==1||h.mint!==input.mint||h.quoteMint!==input.quoteMint||h.slot!==input.minSlot)return null;
  const mint=new web3.PublicKey(input.mint),quote=new web3.PublicKey(input.quoteMint);
  if(['pump_native_curve','sol_to_pump_curve'].includes(input.route)&&h.launchpad==='pump.fun'
    &&pump.bondingCurvePda(mint).toBase58()===h.pool)return h;
  if(['stonk_native_curve','sol_to_stonk_curve'].includes(input.route)&&h.launchpad==='stonk'
    &&input.pool===h.pool&&web3.PublicKey.findProgramAddressSync([Buffer.from('pool'),mint.toBuffer(),quote.toBuffer()],PROGRAM)[0].toBase58()===h.pool){
    if(h.config)new web3.PublicKey(h.config);
    return h;
  }
  return null;
}
const common=[pump.GLOBAL_PDA,pump.PUMP_FEE_CONFIG_PDA,PLATFORM];
function shared(input){
  const h=hint(input);
  return [...common,...(h?.launchpad==='stonk'&&h.config?[new web3.PublicKey(h.config)]:[])];
}
module.exports={hint,shared,common};
