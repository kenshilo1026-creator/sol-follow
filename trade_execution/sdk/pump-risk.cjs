'use strict';
const pump=require('@pump-fun/pump-sdk'),BN=require('bn.js'),{PublicKey}=require('@solana/web3.js');
const risk=require('./risk.cjs');
function checkFees(input,{global,feeConfig,mintSupply,curve,amount,expected,sell=false}){
  // Pinned Pump SDK getFee uses the standard 1e15 supply for non-Mayhem sells.
  const fees=pump.computeFeesBps({global,feeConfig,
    mintSupply:sell&&!curve.isMayhemMode?new BN('1000000000000000'):mintSupply,
    virtualQuoteReserves:curve.virtualQuoteReserves,virtualTokenReserves:curve.virtualTokenReserves,
    quoteMint:curve.quoteMint,creatorFeeBps:curve.creatorFeeBps});
  const bps=BigInt(fees.protocolFeeBps.add(curve.creator.equals(PublicKey.default)?new BN(0):fees.creatorFeeBps).toString());
  const budget=BigInt(amount.toString());
  let feeRate;
  if(sell){
    const gross=budget*BigInt(curve.virtualQuoteReserves.toString())/(BigInt(curve.virtualTokenReserves.toString())+budget);
    feeRate=risk.ppm(gross-BigInt(expected.toString()),gross);
  }else{
    const effective=(budget-1n)*10000n/(10000n+bps);
    feeRate=risk.ppm(budget-effective,budget);
  }
  return risk.checkFees(input,[feeRate],[bps*100n]);
}
module.exports={checkFees};
