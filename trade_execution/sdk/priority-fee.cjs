'use strict';
const web3=require('@solana/web3.js');
const MAX_UNITS=1400000, SCALE=1000000n;
function budget(input){
  const raw=input.priorityFeeLamports;
  if(raw===undefined)return null; // legacy standalone SDK callers
  if(typeof raw!=='string'||!/^\d{1,20}$/.test(raw)||BigInt(raw)>18446744073709551615n)
    throw Error('invalid-priority-fee');
  return BigInt(raw);
}
function price(amount,units,simulation=false){
  // Simulation reserves at least the budget. Final fee rounds down the CU
  // price so ceil(price * limit / 1e6) never exceeds the user's total budget.
  const n=simulation&&amount>0n?(amount-1n)*SCALE+1n:amount*SCALE, d=BigInt(units);
  const result=simulation?(n+d-1n)/d:n/d;
  if(result>18446744073709551615n)throw Error('invalid-priority-fee');
  return result;
}
function prefix(amount,units,simulation=false){
  return [web3.ComputeBudgetProgram.setComputeUnitLimit({units}),
    ...(amount===null?[]:[web3.ComputeBudgetProgram.setComputeUnitPrice({microLamports:price(amount,units,simulation)})])];
}
function detail(amount,units){
  if(amount===null)return {};
  const p=price(amount,units);
  return {priorityFee:{budgetLamports:amount.toString(),microLamports:p.toString(),
    chargedLamports:((p*BigInt(units)+SCALE-1n)/SCALE).toString()}};
}
module.exports={MAX_UNITS,budget,price,prefix,detail};
