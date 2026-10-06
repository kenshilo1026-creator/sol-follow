'use strict';
const SCALE=1000000n;
const ppm=(n,d)=>{n=BigInt(n);d=BigInt(d);if(n<0n||d<=0n||n>d)throw Error('invalid-risk-data');return (n*SCALE+d-1n)/d;};
function combined(values){let keep=SCALE;for(const v of values){const n=BigInt(v);if(n<0n||n>SCALE)throw Error('invalid-risk-data');keep=keep*(SCALE-n)/SCALE;}return SCALE-keep;}
function impact(actual,ideal){actual=BigInt(actual);ideal=BigInt(ideal);if(ideal<=0n||actual<0n)throw Error('invalid-risk-data');return actual>=ideal?0n:ppm(ideal-actual,ideal);}
function check(input,fees,impacts,poolFees=fees){
  const limits=input.risk||{poolFeeBps:200,totalFeeBps:300,impactBps:200};
  for(const n of Object.values(limits))if(!Number.isInteger(n)||n<0||n>2000)throw Error('invalid-risk-data');
  if(poolFees.some(x=>BigInt(x)>BigInt(limits.poolFeeBps)*100n))throw Error('pool-fee-limit');
  if(combined(fees)>BigInt(limits.totalFeeBps)*100n)throw Error('total-fee-limit');
  if(combined(impacts)>BigInt(limits.impactBps)*100n)throw Error('price-impact-limit');
  return {feePpm:combined(fees).toString(),impactPpm:combined(impacts).toString()};
}
module.exports={ppm,combined,impact,check};
