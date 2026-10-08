'use strict';
// Canonical Pump migration pools only. Use the pinned official offline SDK.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const amm=require('@pump-fun/pump-swap-sdk'),risk=require('./risk.cjs');
function owned(row,owner,minSize){
  if(!row)throw Error('graduated-pool-unavailable');
  if(row.executable||!row.owner.equals(owner)||row.data.length<minSize)throw Error('pumpswap-pool-rejected');
}
function quote(input,{pool,globalConfig,feeConfig,base,baseReserve,quoteReserve,amount}){
  const virtual=pool.virtualQuoteReserves;
  if(baseReserve.lten(0)||quoteReserve.lten(0)||quoteReserve.add(virtual).lten(0))throw Error('pumpswap-quote-rejected');
  const args={globalConfig,feeConfig,baseMint:pool.baseMint,baseMintAccount:base,
    creator:pool.creator,coinCreator:pool.coinCreator,baseReserve,quoteReserve,virtualQuoteReserves:virtual,
    quoteMint:pool.quoteMint,isMayhemMode:pool.isMayhemMode,creatorFeeBps:pool.creatorFeeBps};
  let result,fees;
  try{
    result=amm.sellBaseInput({...args,base:amount,slippage:0});
    fees=amm.computeFeesBps({...args,baseMintSupply:new BN(base.supply.toString()),quoteReserve:quoteReserve.add(virtual)});
  }catch{throw Error('pumpswap-quote-rejected');}
  if(result.uiQuote.lten(0))throw Error('pumpswap-quote-rejected');
  // Buyback splits protocol fees; adding it again would double-charge the cap.
  const bps=fees.lpFeeBps.add(fees.protocolFeeBps).add(pool.coinCreator.equals(web3.PublicKey.default)?new BN(0):fees.creatorFeeBps);
  const gross=BigInt(result.internalQuoteAmountOut.toString()),net=BigInt(result.uiQuote.toString());
  const metrics=risk.checkFees(input,[risk.ppm(gross-net,gross)],[BigInt(bps.toString())*100n]);
  return {expected:result.uiQuote,metrics};
}
async function prepare(input,{connection,user,mint,quoteMint,baseProgram,quoteProgram,base,target,out,sourceRow,quoteRow,amount}){
  const poolKey=amm.canonicalPumpPoolPda(mint,quoteMint);
  const baseVault=spl.getAssociatedTokenAddressSync(mint,poolKey,true,baseProgram);
  const quoteVault=spl.getAssociatedTokenAddressSync(quoteMint,poolKey,true,quoteProgram);
  const rows=await connection.getMultipleAccountsInfo([poolKey,amm.GLOBAL_CONFIG_PDA,amm.PUMP_AMM_FEE_CONFIG_PDA,baseVault,quoteVault]);
  owned(rows[0],amm.PUMP_AMM_PROGRAM_ID,211);owned(rows[1],amm.PUMP_AMM_PROGRAM_ID,313);
  owned(rows[2],amm.PUMP_FEE_PROGRAM_ID,amm.FEE_CONFIG_SIZE_PRE_STABLE);
  let pool,globalConfig,feeConfig;
  try{
    pool=amm.PUMP_AMM_SDK.decodePool(rows[0]);globalConfig=amm.PUMP_AMM_SDK.decodeGlobalConfig(rows[1]);
    feeConfig=amm.PUMP_AMM_SDK.decodeFeeConfig(rows[2]);
  }catch{throw Error('pumpswap-pool-rejected');}
  const authority=amm.pumpPoolAuthorityPda(mint);
  const [,bump]=web3.PublicKey.findProgramAddressSync([Buffer.from('pool'),Buffer.alloc(2),authority.toBuffer(),mint.toBuffer(),quoteMint.toBuffer()],amm.PUMP_AMM_PROGRAM_ID);
  if(pool.index!==0||pool.poolBump!==bump||!pool.creator.equals(authority)||!pool.baseMint.equals(mint)||!pool.quoteMint.equals(quoteMint)
    ||!pool.lpMint.equals(amm.lpMintPda(poolKey))||!pool.poolBaseTokenAccount.equals(baseVault)||!pool.poolQuoteTokenAccount.equals(quoteVault))
    throw Error('pumpswap-pool-rejected');
  if(globalConfig.disableFlags&16)throw Error('pumpswap-sell-disabled');
  const reserves=[baseVault,quoteVault].map((address,i)=>{
    const program=i?quoteProgram:baseProgram;owned(rows[i+3],program,spl.ACCOUNT_SIZE);
    const value=spl.unpackAccount(address,rows[i+3],program);
    if(!value.owner.equals(poolKey)||!value.mint.equals(i?quoteMint:mint)||!value.isInitialized||value.isFrozen)
      throw Error('pumpswap-pool-rejected');
    return new BN(value.amount.toString());
  });
  const details=quote(input,{pool,globalConfig,feeConfig,base,baseReserve:reserves[0],quoteReserve:reserves[1],amount});
  const state={poolKey,poolAccountInfo:rows[0],pool,globalConfig,feeConfig,baseMint:mint,baseMintAccount:base,
    poolBaseAmount:reserves[0],poolQuoteAmount:reserves[1],baseTokenProgram:baseProgram,quoteTokenProgram:quoteProgram,
    user,userBaseTokenAccount:target,userQuoteTokenAccount:out,userBaseAccountInfo:sourceRow,userQuoteAccountInfo:quoteRow};
  return {...details,state};
}
async function instructions(route,amount,minOut){
  const ixs=await amm.PUMP_AMM_SDK.sellInstructions(route.state,amount,minOut);
  // The official SDK closes existing WSOL too; retain it and sweep only the receipt gain.
  return ixs.filter(ix=>!(route.state.userQuoteAccountInfo&&ix.programId.equals(spl.TOKEN_PROGRAM_ID)
    &&ix.data.length===1&&ix.data[0]===9&&ix.keys[0].pubkey.equals(route.state.userQuoteTokenAccount)));
}
module.exports={prepare,instructions,quote};
