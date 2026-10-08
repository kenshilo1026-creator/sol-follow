'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const amm=require('@pump-fun/pump-swap-sdk'),BN=require('bn.js');
const {build,REASONS}=require('../../trade_execution/sdk/build.cjs');
const adapter=require('../../trade_execution/sdk/pumpswap-sell.cjs');
const ref=require('../fixtures/pumpswap_sell_reference.json'),snapshot=require('../fixtures/pumpswap_sell_accounts.json');
const a=ref.transaction.message.instructions[3].accounts,pk=x=>new web3.PublicKey(x);
function values(f=snapshot){return Object.fromEntries(Object.entries(f.accounts).filter(([,r])=>r).map(([k,r])=>
  [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));}
function setup(native=true,existing=true,originalUser=false){
  const rows=values(),user=originalUser?pk(a[1]):web3.Keypair.generate().publicKey;
  let mint=pk(a[3]),quote=pk(a[4]);
  if(!native){
    const r=require('../fixtures/pump_non_sol_buy.json').transaction.message.instructions[7].accounts;
    Object.assign(rows,values(require('../fixtures/pump_route_accounts.json')));mint=pk(r[1]);quote=pk(r[2]);
  }
  const baseProgram=rows[mint.toBase58()].owner,quoteProgram=rows[quote.toBase58()].owner;
  const curve=pump.bondingCurvePda(mint),pool=amm.canonicalPumpPoolPda(mint,quote);
  rows[curve.toBase58()].data[48]=1;
  const target=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram),out=spl.getAssociatedTokenAddressSync(quote,user,false,quoteProgram);
  const baseVault=spl.getAssociatedTokenAddressSync(mint,pool,true,baseProgram),quoteVault=spl.getAssociatedTokenAddressSync(quote,pool,true,quoteProgram);
  const amount=14213352207018n;
  function token(mint,amount,owner,program){
    const data=Buffer.alloc(165);mint.toBuffer().copy(data);owner.toBuffer().copy(data,32);data.writeBigUInt64LE(amount,64);data[108]=1;
    return {data,owner:program,executable:false,lamports:2000000};
  }
  if(!native){
    const data=Buffer.from(rows[a[0]].data),authority=amm.pumpPoolAuthorityPda(mint);
    data[8]=web3.PublicKey.findProgramAddressSync([Buffer.from('pool'),Buffer.alloc(2),authority.toBuffer(),mint.toBuffer(),quote.toBuffer()],amm.PUMP_AMM_PROGRAM_ID)[1];
    data.writeUInt16LE(0,9);
    for(const [off,k] of [[11,authority],[43,mint],[75,quote],[107,amm.lpMintPda(pool)],[139,baseVault],[171,quoteVault]])k.toBuffer().copy(data,off);
    data.fill(0,243); // No virtual reserves/cashback/custom creator fee in this synthetic pool.
    rows[pool.toBase58()]={...rows[a[0]],data};
    rows[baseVault.toBase58()]=token(mint,600000000000000n,pool,baseProgram);
    rows[quoteVault.toBase58()]=token(quote,2000000000n,pool,quoteProgram);
  }
  rows[target.toBase58()]=token(mint,amount+4000n,user,baseProgram);
  if(existing)rows[out.toBase58()]=token(quote,50n,user,quoteProgram);else delete rows[out.toBase58()];
  const control={badInput:false,badOutput:false,simulationError:null,simulations:0};
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>rows[k.toBase58()]||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
    async simulateTransaction(tx,options){
      control.simulations++;
      return {value:{err:control.simulationError,unitsConsumed:110000,accounts:options.accounts.addresses.map(k=>{
        const row=k===target.toBase58()?token(mint,control.badInput?3999n:4000n,user,baseProgram):
          token(quote,control.badOutput?50n:1000000000000000n,user,quoteProgram);
        return {...row,owner:row.owner.toBase58(),data:[row.data.toString('base64'),'base64']};
      })}};
    },
  };
  const input={side:'sell',route:native?'pump_native_curve':'sol_to_pump_curve',wallet:user.toBase58(),mint:mint.toBase58(),
    quoteMint:quote.toBase58(),tokenProgram:baseProgram.toBase58(),quoteProgram:quoteProgram.toBase58(),pool:curve.toBase58(),
    lookupTables:[],minSlot:0,amount:amount.toString(),slippagePercent:'2',commitment:'confirmed',
    risk:{poolFeeBps:200,totalFeeBps:300,impactBps:200,tokenTaxBps:200}};
  return {rows,user,mint,quote,curve,pool,baseVault,quoteVault,target,out,input,connection,control};
}

test('sample sell quote reproduces net SOL, virtual reserves and protocol buyback split without double counting',()=>{
  const rows=values(),encoded=ref.meta.logMessages.find(s=>s.startsWith('Program data:')).split(': ')[1];
  const event=amm.OFFLINE_PUMP_AMM_PROGRAM.coder.events.decode(encoded).data;
  const pool=amm.PUMP_AMM_SDK.decodePool(rows[a[0]]);
  pool.virtualQuoteReserves=event.virtualQuoteReserves;
  const base=spl.unpackMint(pk(a[3]),rows[a[3]],rows[a[3]].owner);base.supply=BigInt(event.baseSupply.toString());
  const result=adapter.quote({}, {pool,globalConfig:amm.PUMP_AMM_SDK.decodeGlobalConfig(rows[a[2]]),
    feeConfig:amm.PUMP_AMM_SDK.decodeFeeConfig(rows[amm.PUMP_AMM_FEE_CONFIG_PDA.toBase58()]),base,
    baseReserve:event.poolBaseTokenReserves,quoteReserve:event.poolQuoteTokenReserves,amount:event.baseAmountIn});
  assert.equal(result.expected.toString(),'619363185');assert.equal(result.expected.toString(),event.userQuoteAmountOut.toString());
  assert(event.virtualQuoteReserves.gtn(0));assert.equal(event.minQuoteAmountOut.toString(),'0');
  assert(BigInt(result.metrics.feePpm)>=12500n&&BigInt(result.metrics.feePpm)<12510n);
});

for(const [native,existing] of [[true,false],[true,true],[false,false],[false,true]]){
  test(`graduated Pump ${native?'SOL':'non-SOL'} exit, existing quote ${existing}`,async t=>{
    t.mock.method(global,'fetch',async()=>assert.fail('offline; no quote API or public RPC'));
    const f=setup(native,existing),result=await build(f.input,f.connection);
    assert.equal(result.venue,'pump_swap');assert.equal(result.side,'sell');assert.equal(result.quoteMint,f.input.quoteMint);
    assert.equal(BigInt(result.minOut),(BigInt(result.quotedOut)*98n+99n)/100n);assert(BigInt(result.minOut)>0n);
    const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
    assert(tx.signatures[0].every(x=>x===0));assert.equal(tx.message.header.numRequiredSignatures,1);
    const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
    const ix=ixs.find(x=>x.programId.equals(amm.PUMP_AMM_PROGRAM_ID)&&x.data.length===24);
    assert(ix.keys[0].pubkey.equals(f.pool));assert.equal(ix.data.readBigUInt64LE(8),BigInt(f.input.amount));
    assert.equal(ix.data.readBigUInt64LE(16),BigInt(result.minOut));
    assert(!ixs.some(x=>x.programId.equals(pump.PUMP_PROGRAM_ID)||x.programId.equals(web3.SystemProgram.programId)));
    assert.equal(ixs.filter(x=>x.programId.equals(spl.TOKEN_PROGRAM_ID)&&x.data[0]===9).length,native&&!existing?1:0);
    f.control.badInput=true;await assert.rejects(build(f.input,f.connection),/sell-fill-rejected/);f.control.badInput=false;
    if(!native||existing){f.control.badOutput=true;await assert.rejects(build(f.input,f.connection),/sell-fill-rejected/);f.control.badOutput=false;}
    f.control.simulationError={InstructionError:[1,'failed']};await assert.rejects(build(f.input,f.connection),/simulation-rejected/);
  });
}

test('SDK accounts match sample including pool-v2 and buyback accounts; minOut is rebuilt',async t=>{
  t.mock.method(Math,'random',()=>0);
  const f=setup(true,false,true),result=await build(f.input,f.connection);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  const ix=web3.TransactionMessage.decompile(tx.message).instructions.find(x=>x.programId.equals(amm.PUMP_AMM_PROGRAM_ID)&&x.data.length===24);
  assert.deepEqual(ix.keys.map(x=>x.pubkey.toBase58()),a);
  assert.equal(ix.data.readBigUInt64LE(8),14213352207018n);assert(ix.data.readBigUInt64LE(16)>0n);
});

test('PumpSwap rejects wrong migration/mints/vaults, disabled sells, unavailable reserves and excessive fees',async()=>{
  for(const [mutate,reason] of [
    [f=>f.input.risk.poolFeeBps=0,'pool-fee-limit'],
    [f=>f.input.risk.totalFeeBps=0,'total-fee-limit'],
    [f=>delete f.rows[f.pool.toBase58()],'graduated-pool-unavailable'],
    [f=>f.rows[f.pool.toBase58()].owner=pump.PUMP_PROGRAM_ID,'pumpswap-pool-rejected'],
    [f=>f.rows[f.pool.toBase58()].data[0]^=1,'pumpswap-pool-rejected'],
    [f=>f.rows[f.pool.toBase58()].data[9]=1,'pumpswap-pool-rejected'],
    [f=>f.rows[f.pool.toBase58()].data[11]^=1,'pumpswap-pool-rejected'],
    [f=>f.rows[f.pool.toBase58()].data[75]^=1,'pumpswap-pool-rejected'],
    [f=>f.rows[f.baseVault.toBase58()].data[32]^=1,'pumpswap-pool-rejected'],
    [f=>f.rows[f.quoteVault.toBase58()].data.writeBigUInt64LE(0n,64),'pumpswap-quote-rejected'],
    [f=>f.rows[amm.GLOBAL_CONFIG_PDA.toBase58()].data[56]|=16,'pumpswap-sell-disabled'],
    [f=>f.rows[amm.PUMP_AMM_FEE_CONFIG_PDA.toBase58()].owner=pump.PUMP_PROGRAM_ID,'pumpswap-pool-rejected'],
  ]){
    const f=setup();mutate(f);await assert.rejects(build(f.input,f.connection),new RegExp(reason));
    assert.equal(f.control.simulations,0);assert(REASONS.has(reason));
  }
});

test('migration not yet created defers then retries; graduated buys remain rejected',async()=>{
  const f=setup(),row=f.rows[f.pool.toBase58()];delete f.rows[f.pool.toBase58()];
  await assert.rejects(build(f.input,f.connection),/graduated-pool-unavailable/);
  f.rows[f.pool.toBase58()]=row;
  assert.equal((await build(f.input,f.connection)).venue,'pump_swap');
  const curveConfig=values(require('../fixtures/pump_native_accounts.json'));
  for(const key of [pump.GLOBAL_PDA,pump.PUMP_FEE_CONFIG_PDA])f.rows[key.toBase58()]=curveConfig[key.toBase58()];
  await assert.rejects(build({...f.input,side:'buy'},f.connection),/curve-graduated/);
});

test('older canonical pool is extended atomically; cashback adds only the SDK-required accounts',async()=>{
  const f=setup(),row=f.rows[f.pool.toBase58()];row.data=Buffer.from(row.data.subarray(0,261));
  row.data[244]=1;
  const result=await build(f.input,f.connection);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
  const programIxs=ixs.filter(ix=>ix.programId.equals(amm.PUMP_AMM_PROGRAM_ID));
  assert.equal(programIxs.length,2);
  assert.equal(programIxs[0].data.length,8); // extend_account precedes sell
  const sell=programIxs[1],volume=amm.userVolumeAccumulatorPda(f.user);
  assert(sell.keys.some(k=>k.pubkey.equals(volume)));
  assert.equal(tx.message.header.numRequiredSignatures,1);
  assert(!ixs.some(x=>x.programId.equals(spl.TOKEN_PROGRAM_ID)&&x.data[0]===9));
});
