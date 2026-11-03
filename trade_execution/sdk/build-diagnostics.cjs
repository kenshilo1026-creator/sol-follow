"use strict";
// Request-local public evidence only. No extra RPC, raw accounts or provider text.
const pump=require('@pump-fun/pump-sdk');
function init(input){
  input._buildEvidence={reads:[]};
  input.traceAccounts=row=>{if(input._buildEvidence.reads.length<16)input._buildEvidence.reads.push(row);};
}
function quote(input,{amount,expected,minOut,curve,supply}){
  input._buildEvidence.quote={amount:amount.toString(),quoted_out:expected.toString(),min_out:minOut.toString(),
    slippage_percent:String(input.slippagePercent),mint_supply:supply.toString(),
    virtual_token_reserves:curve.virtualTokenReserves.toString(),virtual_quote_reserves:curve.virtualQuoteReserves.toString(),
    real_token_reserves:curve.realTokenReserves.toString(),real_quote_reserves:curve.realQuoteReserves.toString()};
}
function snapshot(input){
  const e=input._buildEvidence;if(!e)return {};
  const role=k=>k===input.mint?'mint':k===input.pool?'pool':k===pump.GLOBAL_PDA.toBase58()?'pump-global':
    k===pump.PUMP_FEE_CONFIG_PDA.toBase58()?'pump-fee-config':'other';
  return {...e,source_slot:input.minSlot,local_create:input.localCreate,
    reads:e.reads.map(r=>({...r,accounts:r.accounts.slice(0,32).map(a=>({...a,role:role(a.address)}))})),
    rpc:require('./catchup-budget.cjs').transport()};
}
module.exports={init,quote,snapshot};
