'use strict';
const {AsyncLocalStorage}=require('node:async_hooks');
const contexts=new AsyncLocalStorage();
function withCatchupBudget(input,work){
  const expires=input.buySignalExpiresAtMs;
  const budget=Number.isSafeInteger(expires)&&expires>0
    ? {signalDeadline:performance.now()+Math.max(0,expires-Date.now()-200),retryDeadline:null,transport:[]}:null;
  return contexts.run(budget,work);
}
function catchupPolicy(){
  const budget=contexts.getStore();
  if(!budget)return {deadline:performance.now()+250,rounds:2,pause:round=>round*25};
  // One shared window for this buy, not a new second for each SDK RPC. Leave
  // 200ms of signal lifetime for remaining build/sign/send work (not a guarantee).
  budget.retryDeadline??=Math.min(budget.signalDeadline,performance.now()+1000);
  return {deadline:budget.retryDeadline,rounds:8,pause:round=>Math.min(200,round*50)};
}
function recordTransport(row){const rows=contexts.getStore()?.transport;if(rows&&rows.length<32)rows.push(row);}
module.exports={withCatchupBudget,catchupPolicy,isBuy:()=>!!contexts.getStore(),recordTransport,
  transport:()=>[...(contexts.getStore()?.transport||[])]};
