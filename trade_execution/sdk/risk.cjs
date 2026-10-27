'use strict';
const spl=require('@solana/spl-token');
const SCALE=1000000n;
function feeError(reason,actual,limit){
  const error=Error(reason);
  error.feeDetails={actualPpm:actual.toString(),limitPpm:limit.toString()};
  return error;
}
function feeDetails(error){
  if(!['pool-fee-limit','total-fee-limit','token-tax-limit','quote-token-tax-limit'].includes(error.message))return {};
  const d=error.feeDetails;
  if(!d||!['actualPpm','limitPpm'].every(k=>typeof d[k]==='string'&&/^\d{1,7}$/.test(d[k])&&BigInt(d[k])<=SCALE))return {};
  return {feeDetails:{actualPpm:d.actualPpm,limitPpm:d.limitPpm}};
}
const ppm=(n,d)=>{n=BigInt(n);d=BigInt(d);if(n<0n||d<=0n||n>d)throw Error('invalid-risk-data');return (n*SCALE+d-1n)/d;};
function combined(values){let keep=SCALE;for(const v of values){const n=BigInt(v);if(n<0n||n>SCALE)throw Error('invalid-risk-data');keep=keep*(SCALE-n)/SCALE;}return SCALE-keep;}
function sum(values){return values.reduce((n,v)=>{v=BigInt(v);if(v<0n||v>SCALE)throw Error('invalid-risk-data');return n+v;},0n);}
function impact(actual,ideal){actual=BigInt(actual);ideal=BigInt(ideal);if(ideal<=0n||actual<0n)throw Error('invalid-risk-data');return actual>=ideal?0n:ppm(ideal-actual,ideal);}
function limitsFor(input){
  const limits={quoteTokenTaxBps:390,tokenTaxBps:390,impactBps:200,...input.risk};
  for(const k of ['quoteTokenTaxBps','tokenTaxBps','impactBps'])if(!Number.isInteger(limits[k])||limits[k]<0||limits[k]>2000)throw Error('invalid-risk-data');
  return limits;
}
function checkFees(input,fees){
  // Pool/platform fees remain part of executable quotes, not an admission cap.
  limitsFor(input);
  return {feePpm:sum(fees).toString()};
}
function check(input,fees,impacts){
  const metrics=checkFees(input,fees),limits=limitsFor(input);
  if(combined(impacts)>BigInt(limits.impactBps)*100n)throw Error('price-impact-limit');
  return {...metrics,impactPpm:combined(impacts).toString()};
}
function tax(mint,epoch,amount){
  const config=spl.getTransferFeeConfig(mint);if(!config)return 0n;
  if(!Number.isSafeInteger(epoch)||epoch<0)throw Error('invalid-risk-data');
  return spl.calculateEpochFee(config,BigInt(epoch),BigInt(amount));
}
function taxRate(mint,epoch){
  const config=spl.getTransferFeeConfig(mint);if(!config)return 0n;
  if(!Number.isSafeInteger(epoch)||epoch<0)throw Error('invalid-risk-data');
  return BigInt(Math.max(spl.getEpochFee(config,BigInt(epoch)).transferFeeBasisPoints,
    config.newerTransferFee.transferFeeBasisPoints))*100n;
}
function checkTokenTax(input,mint,epoch,role='target'){
  if(!['target','quote'].includes(role))throw Error('invalid-risk-data');
  const actual=taxRate(mint,epoch),limits=limitsFor(input);
  const limit=BigInt(limits[role==='quote'?'quoteTokenTaxBps':'tokenTaxBps'])*100n;
  if(actual>limit)throw feeError(role==='quote'?'quote-token-tax-limit':'token-tax-limit',actual,limit);
  return actual;
}
module.exports={ppm,combined,sum,impact,check,checkFees,checkTokenTax,feeDetails,tax,taxRate};
