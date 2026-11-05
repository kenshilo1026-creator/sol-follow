"use strict";
// Only protocol settings may use last-known-good snapshots. Never pool reserves,
// token mints (including transfer fees), wallet balances or price oracles.
const crypto=require('node:crypto'),pump=require('@pump-fun/pump-sdk');
const {PROGRAM,PLATFORM}=require('./stonk.cjs');
const disc=name=>crypto.createHash('sha256').update('account:'+name).digest().subarray(0,8);
function valid(key,row){
  try{
    if(!row||row.executable||!Buffer.isBuffer(row.data))return false;
    if(key===pump.GLOBAL_PDA.toBase58()){
      if(!row.owner.equals(pump.PUMP_PROGRAM_ID))return false;
      pump.PUMP_SDK.decodeGlobal(row);return true;
    }
    if(key===pump.PUMP_FEE_CONFIG_PDA.toBase58()){
      if(!row.owner.equals(pump.PUMP_FEE_PROGRAM_ID))return false;
      pump.PUMP_SDK.decodeFeeConfig(row);return true;
    }
    const platform=key===PLATFORM.toBase58();
    return row.owner.equals(PROGRAM)&&row.data.length>=(platform?728:115)
      &&row.data.subarray(0,8).equals(disc(platform?'PlatformConfig':'GlobalConfig'));
  }catch{return false;}
}
module.exports={valid};
