'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),crypto=require('node:crypto');
const {build,REASONS}=require('../../trade_execution/sdk/build.cjs');
const cpmm=require('../../trade_execution/sdk/cpmm-sell.cjs'),stonk=require('../../trade_execution/sdk/stonk.cjs');
const ref=require('../fixtures/graduated_sell_reference.json'),snapshot=require('../fixtures/graduated_cpmm_accounts.json');
const sample=ref.meta.innerInstructions.flatMap(x=>x.instructions).find(x=>x.programId===cpmm.PROGRAM.toBase58());
const pk=x=>new web3.PublicKey(x),mint=pk(sample.accounts[10]),quote=pk(sample.accounts[11]);
const launch=web3.PublicKey.findProgramAddressSync([Buffer.from('pool'),mint.toBuffer(),quote.toBuffer()],stonk.PROGRAM)[0];
const disc=(ns,name)=>crypto.createHash('sha256').update(ns+':'+name).digest().subarray(0,8);
function rowsFor(fixture=snapshot){return Object.fromEntries(Object.entries(fixture.accounts).filter(([,r])=>r).map(([k,r])=>
  [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));}
function setup(native=false,existingWsol=true){
  const rows=rowsFor(),user=web3.Keypair.generate().publicKey;
  let base=mint,outMint=quote,oldPool=launch;
  if(native){
    // Synthetic SOL pair exercises the other sorted-mint direction using real mint/LaunchLab layouts.
    const fixture=require('../fixtures/stonk_native_accounts.json'),reference=require('../fixtures/stonk_native_buy.json');
    const a=reference.transaction.message.instructions[6].accounts;
    Object.assign(rows,rowsFor(fixture));base=pk(a[9]);outMint=spl.NATIVE_MINT;oldPool=pk(a[4]);
    rows[oldPool.toBase58()].data[17]=2;rows[oldPool.toBase58()].data[20]=1;
    if(!rows[outMint.toBase58()]){const data=Buffer.alloc(82);data[44]=9;data[45]=1;
      rows[outMint.toBase58()]={data,owner:spl.TOKEN_PROGRAM_ID,executable:false,lamports:1};}
  }
  const baseProgram=rows[base.toBase58()].owner,quoteProgram=rows[outMint.toBase58()].owner;
  const config=pk(sample.accounts[2]),a=cpmm.addresses(config,base,outMint);
  const target=spl.getAssociatedTokenAddressSync(base,user,false,baseProgram),out=spl.getAssociatedTokenAddressSync(outMint,user,false,quoteProgram);
  const amount=19222990878600n;
  function token(mint,amount,owner,program){
    const data=Buffer.alloc(165);mint.toBuffer().copy(data);owner.toBuffer().copy(data,32);data.writeBigUInt64LE(amount,64);data[108]=1;
    return {data,owner:program,executable:false,lamports:2000000};
  }
  if(native){
    const d=Buffer.from(rows[sample.accounts[3]].data),zero=base.equals(a.mint0);
    for(const [off,k] of [[8,config],[72,a.vault0],[104,a.vault1],[168,a.mint0],[200,a.mint1],
      [232,zero?baseProgram:quoteProgram],[264,zero?quoteProgram:baseProgram],[296,a.observation]])k.toBuffer().copy(d,off);
    d[331]=zero?6:9;d[332]=zero?9:6;d[389]=0;
    for(const off of [341,349,357,365,397,405])d.writeBigUInt64LE(0n,off);
    rows[a.pool.toBase58()]={...rows[sample.accounts[3]],data:d};
    rows[a.vault0.toBase58()]=token(a.mint0,zero?900000000000000n:100000000000n,cpmm.AUTHORITY,zero?baseProgram:quoteProgram);
    rows[a.vault1.toBase58()]=token(a.mint1,zero?100000000000n:900000000000000n,cpmm.AUTHORITY,zero?quoteProgram:baseProgram);
    const oracle=Buffer.from(rows[sample.accounts[12]].data);a.pool.toBuffer().copy(oracle,11);
    rows[a.observation.toBase58()]={...rows[sample.accounts[12]],data:oracle};
    config.toBuffer().copy(rows[stonk.PLATFORM.toBase58()].data,688);
  }
  rows[target.toBase58()]=token(base,amount+4000n,user,baseProgram);
  if(!native||existingWsol)rows[out.toBase58()]=token(outMint,50n,user,quoteProgram);
  else delete rows[out.toBase58()];
  const control={badInput:false,badOutput:false,simulationError:null,simulations:0};
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>rows[k.toBase58()]||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
    async simulateTransaction(tx,options){
      control.simulations++;
      return {value:{err:control.simulationError,unitsConsumed:90000,accounts:options.accounts.addresses.map(k=>{
        const r=k===target.toBase58()?token(base,control.badInput?3999n:4000n,user,baseProgram):
          token(outMint,control.badOutput?50n:1000000000000000n,user,quoteProgram);
        return {...r,owner:r.owner.toBase58(),data:[r.data.toString('base64'),'base64']};
      })}};
    },
  };
  const input={side:'sell',route:native?'stonk_native_curve':'sol_to_stonk_curve',wallet:user.toBase58(),mint:base.toBase58(),
    quoteMint:outMint.toBase58(),tokenProgram:baseProgram.toBase58(),quoteProgram:quoteProgram.toBase58(),pool:oldPool.toBase58(),
    lookupTables:[],minSlot:0,amount:amount.toString(),slippagePercent:'2',commitment:'confirmed',
    risk:{poolFeeBps:200,totalFeeBps:300,impactBps:200,tokenTaxBps:200}};
  return {rows,user,base,outMint,oldPool,a,target,out,input,connection,control};
}

test('reference CPMM event reproduces exact 1% taxed input and creator-on-output quote',()=>{
  const data=Buffer.from(ref.meta.logMessages.find(s=>s.startsWith('Program data: QMbN')).split(': ')[1],'base64');
  assert(pk(data.subarray(8,40)).equals(pk(sample.accounts[3])));
  const value=cpmm.quoteExactIn({amount:data.readBigUInt64LE(56)+data.readBigUInt64LE(72),
    reserveIn:data.readBigUInt64LE(40),reserveOut:data.readBigUInt64LE(48),tradeRate:2500n,creatorRate:10000n,
    creatorOnInput:false,inputTax:data.readBigUInt64LE(72)});
  assert.equal(value.net,30244461n);assert.equal(value.net,data.readBigUInt64LE(64));
  assert.equal(value.poolFee,12475n);
  const user=pk(sample.accounts[0]),route={...cpmm.addresses(pk(sample.accounts[2]),mint,quote),
    inputVault:pk(sample.accounts[6]),outputVault:pk(sample.accounts[7])};
  const ix=cpmm.instruction({user,mint,quote,baseProgram:pk(sample.accounts[8]),quoteProgram:pk(sample.accounts[9]),
    target:pk(sample.accounts[4]),out:pk(sample.accounts[5]),amount:19222990878600n,minOut:1n,route});
  assert.deepEqual(ix.keys.map(k=>k.pubkey.toBase58()),sample.accounts);
  assert(ix.data.subarray(0,8).equals(disc('global','swap_base_input')));
  assert.equal(ix.keys.filter(k=>k.isSigner).length,1);
});

for(const [native,existing] of [[false,true],[true,true],[true,false]]){
  test(`graduated Stonk ${native?'SOL':'non-SOL'} exit, existing WSOL ${existing}`,async t=>{
    t.mock.method(global,'fetch',async()=>assert.fail('offline; no Jupiter or external RPC'));
    const f=setup(native,existing),result=await build(f.input,f.connection);
    assert.equal(result.venue,'raydium_cpmm');assert.equal(result.side,'sell');
    assert.equal(result.amount,f.input.amount);assert.equal(result.quoteMint,f.input.quoteMint);
    assert.equal(BigInt(result.minOut),(BigInt(result.quotedOut)*98n+99n)/100n);
    assert(BigInt(result.risk.feePpm)>20000n&&BigInt(result.risk.feePpm)<30000n);
    const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
    assert.equal(tx.message.header.numRequiredSignatures,1);assert(tx.signatures[0].every(x=>x===0));
    const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
    const sell=ixs.find(ix=>ix.programId.equals(cpmm.PROGRAM));
    assert(sell.keys[3].pubkey.equals(f.a.pool));assert.equal(sell.data.readBigUInt64LE(8),BigInt(f.input.amount));
    assert.equal(sell.data.readBigUInt64LE(16),BigInt(result.minOut));
    assert(!ixs.some(ix=>ix.programId.equals(stonk.PROGRAM)||ix.programId.equals(web3.SystemProgram.programId)));
    assert.equal(ixs.filter(ix=>ix.programId.equals(spl.TOKEN_PROGRAM_ID)&&ix.data[0]===9).length,native&&!existing?1:0);
    f.control.badInput=true;await assert.rejects(build(f.input,f.connection),/sell-fill-rejected/);f.control.badInput=false;
    if(!native||existing){f.control.badOutput=true;await assert.rejects(build(f.input,f.connection),/sell-fill-rejected/);f.control.badOutput=false;}
    f.control.simulationError={InstructionError:[1,'failed']};await assert.rejects(build(f.input,f.connection),/simulation-rejected/);
  });
}

test('graduated exit enforces fees, tax, migration proof, vaults, clock and pool availability before signing',async()=>{
  for(const [mutate,reason] of [
    [f=>f.input.risk.poolFeeBps=100,'pool-fee-limit'],
    [f=>f.input.risk.totalFeeBps=200,'total-fee-limit'],
    [f=>f.input.risk.tokenTaxBps=99,'token-tax-limit'],
    [f=>f.rows[f.oldPool.toBase58()].data[17]=1,'stonk-pool-rejected'],
    [f=>f.rows[f.oldPool.toBase58()].data[20]=0,'unsupported-graduation'],
    [f=>f.rows[f.oldPool.toBase58()].data[173]^=1,'stonk-pool-rejected'],
    [f=>delete f.rows[f.a.pool.toBase58()],'graduated-pool-unavailable'],
    [f=>f.rows[f.a.pool.toBase58()].owner=stonk.PROGRAM,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.pool.toBase58()].data[168]^=1,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.pool.toBase58()].data[329]=4,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.pool.toBase58()].data[389]=3,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.pool.toBase58()].data.writeBigUInt64LE(0xffffffffffffffffn,373),'cpmm-pool-not-open'],
    [f=>f.rows[f.a.vault0.toBase58()].data[32]^=1,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.vault0.toBase58()].data.writeBigUInt64LE(0n,64),'cpmm-quote-rejected'],
    [f=>f.rows[f.a.config.toBase58()].data[10]^=1,'cpmm-pool-rejected'],
    [f=>f.rows[f.a.observation.toBase58()].data[11]^=1,'cpmm-pool-rejected'],
  ]){
    const f=setup();mutate(f);await assert.rejects(build(f.input,f.connection),new RegExp(reason));
    assert.equal(f.control.simulations,0);assert(REASONS.has(reason));
  }
});

test('migration in progress defers; retry discovers CPMM; buy remains pre-graduation only',async()=>{
  const f=setup();f.rows[f.oldPool.toBase58()].data[17]=1;
  await assert.rejects(build(f.input,f.connection),/stonk-pool-rejected/);
  f.rows[f.oldPool.toBase58()].data[17]=2;
  assert.equal((await build(f.input,f.connection)).venue,'raydium_cpmm');
  await assert.rejects(build({...f.input,side:'buy'},f.connection),/stonk-pool-rejected/);
});
