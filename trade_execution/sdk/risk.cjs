'use strict';
const SCALE=1000000n;
function feeError(reason,actual,limit){
  const error=Error(reason);
  error.feeDetails={actualPpm:actual.toString(),limitPpm:limit.toString()};
  return error;
}
function feeDetails(error){
  if(!['pool-fee-limit','total-fee-limit','token-tax-limit'].includes(error.message))return {};
  const d=error.feeDetails;
  if(!d||!['actualPpm','limitPpm'].every(k=>typeof d[k]==='string'&&/^\d{1,7}$/.test(d[k])&&BigInt(d[k])<=SCALE))return {};
  return {feeDetails:{actualPpm:d.actualPpm,limitPpm:d.limitPpm}};
}
const ppm=(n,d)=>{n=BigInt(n);d=BigInt(d);if(n<0n||d<=0n||n>d)throw Error('invalid-risk-data');return (n*SCALE+d-1n)/d;};
function combined(values){let keep=SCALE;for(const v of values){const n=BigInt(v);if(n<0n||n>SCALE)throw Error('invalid-risk-data');keep=keep*(SCALE-n)/SCALE;}return SCALE-keep;}
function impact(actual,ideal){actual=BigInt(actual);ideal=BigInt(ideal);if(ideal<=0n||actual<0n)throw Error('invalid-risk-data');return actual>=ideal?0n:ppm(ideal-actual,ideal);}
function limitsFor(input){
  const limits=input.risk||{poolFeeBps:200,totalFeeBps:300,impactBps:200};
  for(const n of Object.values(limits))if(!Number.isInteger(n)||n<0||n>2000)throw Error('invalid-risk-data');
  for(const k of ['poolFeeBps','totalFeeBps','impactBps'])if(!Number.isInteger(limits[k]))throw Error('invalid-risk-data');
  return limits;
}
function checkFees(input,fees,poolFees=fees){
  const limits=limitsFor(input);
  const exceeded=poolFees.find(x=>BigInt(x)>BigInt(limits.poolFeeBps)*100n);
  if(exceeded!==undefined)throw feeError('pool-fee-limit',BigInt(exceeded),BigInt(limits.poolFeeBps)*100n);
  if(combined(fees)>BigInt(limits.totalFeeBps)*100n)throw feeError('total-fee-limit',combined(fees),BigInt(limits.totalFeeBps)*100n);
  return {feePpm:combined(fees).toString()};
}
function check(input,fees,impacts,poolFees=fees){
  const metrics=checkFees(input,fees,poolFees),limits=limitsFor(input);
  if(combined(impacts)>BigInt(limits.impactBps)*100n)throw Error('price-impact-limit');
  return {...metrics,impactPpm:combined(impacts).toString()};
}
function checkTokenTax(input,mint,epoch){
  const spl=require('@solana/spl-token'),limit=limitsFor(input).tokenTaxBps??200;
  const transfer=spl.getTransferFeeConfig(mint);
  if(!transfer)return;
  if(!Number.isSafeInteger(epoch)||epoch<0)throw Error('invalid-risk-data');
  // Cap the advertised rate even when maximumFee makes this trade cheaper.
  // Reject a scheduled increase too; ignore an obsolete older rate.
  const rates=[spl.getEpochFee(transfer,BigInt(epoch)),transfer.newerTransferFee];
  const highest=Math.max(...rates.map(x=>x.transferFeeBasisPoints));
  if(highest>limit)throw feeError('token-tax-limit',BigInt(highest)*100n,BigInt(limit)*100n);
}
module.exports={ppm,combined,impact,check,checkFees,checkTokenTax,feeDetails};
