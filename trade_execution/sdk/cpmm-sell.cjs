'use strict';
// Verified Stonk LaunchLab migration -> canonical Raydium CPMM, exact input.
// Layout and integer math: raydium-io/raydium-cp-swap states/{pool,config}.rs,
// curve/calculator.rs and instructions/swap_base_input.rs. No quote API.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),crypto=require('node:crypto');
const stonk=require('./stonk.cjs'),risk=require('./risk.cjs');
const PROGRAM=new web3.PublicKey('CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C');
const pda=(...seeds)=>web3.PublicKey.findProgramAddressSync(seeds,PROGRAM);
const [AUTHORITY,AUTH_BUMP]=pda(Buffer.from('vault_and_lp_mint_auth_seed'));
const disc=(ns,name)=>crypto.createHash('sha256').update(ns+':'+name).digest().subarray(0,8);
const key=(data,offset)=>new web3.PublicKey(data.subarray(offset,offset+32));
const u64=(data,offset)=>data.readBigUInt64LE(offset);
const ceilFee=(amount,rate)=>(amount*rate+999999n)/1000000n;
function account(row,owner,size,name){
  if(!row)throw Error('graduated-pool-unavailable');
  if(row.executable||!row.owner.equals(owner)||row.data.length!==size
    ||(name&&!row.data.subarray(0,8).equals(disc('account',name))))throw Error('cpmm-pool-rejected');
  return row.data;
}
function addresses(config,mint,quote){
  const [mint0,mint1]=[mint,quote].sort((a,b)=>Buffer.compare(a.toBuffer(),b.toBuffer()));
  const pool=pda(Buffer.from('pool'),config.toBuffer(),mint0.toBuffer(),mint1.toBuffer())[0];
  return {config,pool,mint0,mint1,
    vault0:pda(Buffer.from('pool_vault'),pool.toBuffer(),mint0.toBuffer())[0],
    vault1:pda(Buffer.from('pool_vault'),pool.toBuffer(),mint1.toBuffer())[0],
    observation:pda(Buffer.from('observation'),pool.toBuffer())[0]};
}
function quoteExactIn({amount,reserveIn,reserveOut,tradeRate,creatorRate,creatorOnInput,inputTax=0n}){
  for(const rate of [tradeRate,creatorRate])if(rate<0n||rate>=1000000n)throw Error('invalid-risk-data');
  if(tradeRate+creatorRate>=1000000n||reserveIn<=0n||reserveOut<=0n||amount<=inputTax||inputTax<0n)
    throw Error('cpmm-quote-rejected');
  const net=amount-inputTax,feeIn=ceilFee(net,tradeRate+(creatorOnInput?creatorRate:0n));
  const effective=net-feeIn;
  if(effective<=0n)throw Error('cpmm-quote-rejected');
  const gross=effective*reserveOut/(reserveIn+effective);
  const feeOut=creatorOnInput?0n:ceilFee(gross,creatorRate);
  if(gross<=feeOut)throw Error('cpmm-quote-rejected');
  return {net:gross-feeOut,fees:[risk.ppm(inputTax,amount),risk.ppm(feeIn,net),risk.ppm(feeOut,gross)],
    // One pool: combine its input/output rates, not two separate pool caps.
    poolFee:creatorOnInput?tradeRate+creatorRate:risk.combined([tradeRate,creatorRate])};
}
async function prepare(input,{connection,state,mint,quote,baseProgram,quoteProgram,base,quoteInfo,amount}){
  if(state.status!==2||state.migrateType!==1)throw Error('unsupported-graduation');
  const [platform]=await connection.getMultipleAccountsInfo([stonk.PLATFORM]);
  // Platform layout has appended fields; the verified prefix remains stable.
  if(!platform||platform.executable||!platform.owner.equals(stonk.PROGRAM)||platform.data.length<720
    ||!platform.data.subarray(0,8).equals(disc('account','PlatformConfig'))
    ||!key(platform.data,16).equals(stonk.FEE_WALLET))throw Error('cpmm-pool-rejected');
  const a=addresses(key(platform.data,688),mint,quote);
  const rows=await connection.getMultipleAccountsInfo([a.pool,a.config,a.vault0,a.vault1,a.observation,web3.SYSVAR_CLOCK_PUBKEY]);
  const d=account(rows[0],PROGRAM,637,'PoolState'),c=account(rows[1],PROGRAM,236,'AmmConfig');
  const zero=mint.equals(a.mint0),program0=zero?baseProgram:quoteProgram,program1=zero?quoteProgram:baseProgram;
  for(const [off,expected] of [[8,a.config],[72,a.vault0],[104,a.vault1],[168,a.mint0],[200,a.mint1],
    [232,program0],[264,program1],[296,a.observation]])if(!key(d,off).equals(expected))throw Error('cpmm-pool-rejected');
  const index=Buffer.alloc(2);index.writeUInt16BE(c.readUInt16LE(10));
  const [configKey,configBump]=pda(Buffer.from('amm_config'),index);
  if(!a.config.equals(configKey)||c[8]!==configBump||d[328]!==AUTH_BUMP||d[329]>7||(d[329]&4)
    ||d[389]>2||d[390]>1||d[331]!== (zero?base.decimals:quoteInfo.decimals)
    ||d[332]!== (zero?quoteInfo.decimals:base.decimals))throw Error('cpmm-pool-rejected');
  const clock=account(rows[5],new web3.PublicKey('Sysvar1111111111111111111111111111111111111'),40);
  if(clock.readBigInt64LE(32)<BigInt(u64(d,373)))throw Error('cpmm-pool-not-open');
  const epoch=Number(u64(clock,16));
  risk.checkTokenTax(input,base,epoch);
  const oracle=account(rows[4],PROGRAM,4075,'ObservationState');
  if(!key(oracle,11).equals(a.pool))throw Error('cpmm-pool-rejected');
  const vaults=[a.vault0,a.vault1].map((address,i)=>{
    const program=i?program1:program0,row=rows[i+2];
    if(!row||row.executable||!row.owner.equals(program))throw Error('cpmm-pool-rejected');
    const v=spl.unpackAccount(address,row,program);
    if(!v.mint.equals(i?a.mint1:a.mint0)||!v.owner.equals(AUTHORITY)||!v.isInitialized||v.isFrozen)
      throw Error('cpmm-pool-rejected');
    const reserve=v.amount-u64(d,341+i*8)-u64(d,357+i*8)-u64(d,397+i*8);
    if(reserve<=0n)throw Error('cpmm-quote-rejected');
    return reserve;
  });
  const transfer=spl.getTransferFeeConfig(base),value=BigInt(amount.toString());
  const details=quoteExactIn({amount:value,reserveIn:vaults[zero?0:1],reserveOut:vaults[zero?1:0],
    tradeRate:u64(c,12),creatorRate:d[390]?u64(c,108):0n,
    creatorOnInput:d[389]===0||d[389]===(zero?1:2),
    inputTax:transfer?spl.calculateEpochFee(transfer,BigInt(epoch),value):0n});
  const metrics=risk.checkFees(input,details.fees,[details.poolFee]);
  return {...a,inputVault:zero?a.vault0:a.vault1,outputVault:zero?a.vault1:a.vault0,expected:details.net,metrics};
}
function instruction({user,mint,quote,baseProgram,quoteProgram,target,out,amount,minOut,route}){
  const addresses=[user,AUTHORITY,route.config,route.pool,target,out,route.inputVault,route.outputVault,
    baseProgram,quoteProgram,mint,quote,route.observation],writable=new Set([3,4,5,6,7,12]);
  const data=Buffer.alloc(24);disc('global','swap_base_input').copy(data);
  data.writeBigUInt64LE(BigInt(amount.toString()),8);data.writeBigUInt64LE(BigInt(minOut.toString()),16);
  return new web3.TransactionInstruction({programId:PROGRAM,data,
    keys:addresses.map((pubkey,i)=>({pubkey,isSigner:i===0,isWritable:writable.has(i)}))});
}
module.exports={prepare,instruction,quoteExactIn,addresses,PROGRAM,AUTHORITY};
