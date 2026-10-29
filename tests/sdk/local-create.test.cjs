'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const local=require('../../trade_execution/sdk/local-create.cjs');
const {build}=require('../../trade_execution/sdk/build.cjs');
const {PROGRAM,PLATFORM}=require('../../trade_execution/sdk/stonk.cjs');
const samples=require('../fixtures/local_create_inputs.json');
const sample=prefix=>structuredClone(samples.find(s=>s.file.startsWith(prefix)).input);
const pk=x=>new web3.PublicKey(x);
const load=name=>new Map(Object.entries(require('../fixtures/'+name).accounts).filter(([,v])=>v)
  .map(([k,v])=>[k,{...v,owner:pk(v.owner),data:Buffer.from(v.data[0],'base64')}]));
const mintResponse=input=>{const row=local.project(input).rows.get(input.mint);return {owner:input.tokenProgram,executable:false,data:[row.data.toString('base64'),'base64']};};
const configured=input=>({...input,wallet:web3.Keypair.generate().publicKey.toBase58(),amount:'10000000',
  slippagePercent:'2',minLiquidity:'0',commitment:'processed',priorityFeeLamports:'200000',
  risk:{tokenTaxBps:390,quoteTokenTaxBps:390,impactBps:200}});

test('historical Pump and Stonk events use final reserves; unknown flags use normal builder',()=>{
  for(const s of samples){
    const r=local.project(s.input),f=s.input.createHint.localSnapshot.flags;
    const unsupported=f.mayhem_mode||f.holder_reward||f.uninterpreted_tail_hex;
    assert.equal(r.rows.size,unsupported?0:2,s.file);
  }
  const p=sample('create_CBL'),r=local.project(p),curve=pump.PUMP_SDK.decodeBondingCurve(r.rows.get(p.pool));
  const evt=pump.PUMP_SDK.decodeTradeEventBc(Buffer.from(p.createHint.localSnapshot.events[1],'base64').subarray(8));
  assert.equal(curve.virtualTokenReserves.toString(),evt.virtualTokenReserves.toString());
  assert.equal(curve.realQuoteReserves.toString(),evt.realQuoteReserves.toString());
  const s=sample('create_GcH'),pool=local.project(s).rows.get(s.pool).data;
  const trade=Buffer.from(s.createHint.localSnapshot.events[1],'base64');
  assert.equal(pool.readBigUInt64LE(53),trade.readBigUInt64LE(80));
  assert.notEqual(pool.readBigUInt64LE(53),trade.readBigUInt64LE(64));
  const mint=spl.unpackMint(pk(s.mint),local.project(s).rows.get(s.mint),pk(s.tokenProgram));
  assert.equal(spl.getTransferFeeConfig(mint).newerTransferFee.transferFeeBasisPoints,100);
});

test('mismatched source, missing trade, completion and disabled feature cannot project accounts',()=>{
  for(const change of [p=>p.localCreateEnabled=false,p=>p.createHint.localSnapshot.slot--,
    p=>p.createHint.localSnapshot.mint=web3.Keypair.generate().publicKey.toBase58(),
    p=>p.createHint.localSnapshot.tradeCount++,p=>p.createHint.localSnapshot.events.pop(),
    p=>p.createHint.localSnapshot.events.push(Buffer.alloc(16).toString('base64'))]){
    const p=sample('create_CBL');change(p);assert.equal(local.project(p).rows.size,0);
  }
  const s=sample('create_GcH'),data=Buffer.from(s.createHint.localSnapshot.events[1],'base64');
  data[145]=1;s.createHint.localSnapshot.events[1]=data.toString('base64');assert.equal(local.project(s).rows.size,0);
});

test('Pump local build preserves SDK buy/minOut/priority fee with no mint or curve reads',async()=>{
  const input=configured(sample('create_CBL')),projected=local.project(input).rows,common=load('pump_native_accounts.json');
  let reads=[],simulations=0;
  const connection={async getMultipleAccountsInfo(keys){reads.push(keys.map(String));return keys.map(k=>common.get(String(k))||projected.get(String(k))||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){simulations++;return {context:{slot:input.minSlot},value:{err:null,unitsConsumed:100000,accounts:options.accounts?[mintResponse(input)]:undefined}};}};
  const random=Math.random;Math.random=()=>0;
  try{
    const normal=await build({...input,localCreateEnabled:false},connection);reads=[];
    const quick=await build(input,connection);
    assert.equal(quick.transaction,normal.transaction);assert.equal(quick.minOut,normal.minOut);
    assert.equal(quick.priorityFee.chargedLamports,'200000');assert.equal(simulations,2);
    assert.equal(reads.length,1);assert.deepEqual(reads[0],[pump.GLOBAL_PDA.toBase58(),pump.PUMP_FEE_CONFIG_PDA.toBase58()]);
    assert.deepEqual(quick.localCreate.projectedAccounts,[input.mint,input.pool]);
    // Those two common rows can come from the warm cache: no live mint/pool query.
  }finally{Math.random=random;}
});

function stonkSimulation(tx,input,options,mutation){
  const ixs=web3.TransactionMessage.decompile(tx.message).instructions,buyIndex=ixs.findIndex(ix=>ix.programId.equals(PROGRAM));
  const buy=ixs[buyIndex],a=buy.keys.map(x=>x.pubkey.toBase58()),amount=buy.data.readBigUInt64LE(8),min=buy.data.readBigUInt64LE(16);
  const gross=(min*10000n+9899n)/9900n+100n;
  const moves=[{programId:a[12],parsed:{type:'transferChecked',info:{source:a[6],mint:a[10],destination:a[8],tokenAmount:{amount:amount.toString()}}}},
    {programId:a[11],parsed:{type:'transferChecked',info:{source:a[7],mint:a[9],destination:a[5],tokenAmount:{amount:gross.toString()}}}}];
  moves.push({programId:a[12],parsed:{type:'transferChecked',info:{source:a[8],mint:a[10],destination:a[16],tokenAmount:{amount:(amount/100n).toString()}}}});
  if(mutation==='overspend')moves[0].parsed.info.tokenAmount.amount=(amount+1n).toString();
  if(mutation==='underfill')moves[1].parsed.info.tokenAmount.amount=min.toString();
  if(mutation==='foreign-destination')moves[1].parsed.info.destination=web3.Keypair.generate().publicKey.toBase58();
  const token=Buffer.alloc(165);pk(input.mint).toBuffer().copy(token);pk(input.wallet).toBuffer().copy(token,32);token.writeBigUInt64LE(gross,64);token[108]=1;
  const simulatedMint=Buffer.from(local.project(input).rows.get(input.mint).data);
  if(mutation==='changed-tax')simulatedMint.writeUInt16LE(1000,simulatedMint.length-2);
  const accountData=options.accounts?.addresses[0]===input.mint?simulatedMint:token;
  const value={err:null,unitsConsumed:150000,accounts:[{owner:input.tokenProgram,executable:false,data:[accountData.toString('base64'),'base64']}]};
  if(mutation!=='missing-inner')value.innerInstructions=[{index:buyIndex,instructions:moves}];
  return {context:{slot:input.minSlot},value};
}

test('Stonk native local build needs only shared config, preserves quote and checks actual net transfer',async()=>{
  const input=configured(sample('create_GcH')),projected=local.project(input).rows,common=load('stonk_native_accounts.json');
  let reads=[],epochs=0,simulations=0,mutation;
  const connection={async getMultipleAccountsInfo(keys){reads.push(keys.map(String));return keys.map(k=>projected.get(String(k))||common.get(String(k))||null);},
    async getEpochInfo(){epochs++;return {epoch:2000};},async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){simulations++;return stonkSimulation(tx,input,options,mutation);}};
  const normal=await build({...input,localCreateEnabled:false},connection);reads=[];epochs=0;
  const quick=await build(input,connection);
  assert.equal(quick.minOut,normal.minOut);assert.equal(quick.quotedOut,normal.quotedOut);assert.equal(epochs,0);assert.equal(simulations,2);
  assert.deepEqual(reads,[[input.createHint.config,PLATFORM.toBase58()]]);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(quick.transaction,'base64'));
  const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
  assert(!ixs.some(ix=>ix.programId.equals(spl.TOKEN_PROGRAM_ID)&&ix.data[0]===9),'never close existing wrapped SOL');
  for(mutation of ['overspend','underfill','foreign-destination','missing-inner','changed-tax'])await assert.rejects(build(input,connection),/stonk-fill-rejected|create-state-changed/);
});

test('simulation errors and transfer-tax cap still reject the local Stonk route',async()=>{
  const input=configured(sample('create_GcH')),common=load('stonk_native_accounts.json');
  const conn={async getMultipleAccountsInfo(keys){return keys.map(k=>common.get(String(k))||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(){return {value:{err:{InstructionError:[1,'failure']},unitsConsumed:1000}};}};
  await assert.rejects(build(input,conn),/simulation-rejected/);
  input.risk.tokenTaxBps=50;await assert.rejects(build(input,conn),/token-tax-limit/);
});

test('warm common cache builds both native routes with ZERO account RPC reads',async()=>{
  const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
  const fast=require('../../trade_execution/sdk/create-fast.cjs');
  for(const prefix of ['create_CBL','create_GcH']){
    const input=configured(sample(prefix)),isStonk=input.route==='stonk_native_curve';
    let simulations=0,accountRPC=0;
    const rpc={_rpcWebSocketConnected:true,
      async getMultipleAccountsInfoAndContext(){accountRPC++;throw Error('account RPC forbidden on warm path');},
      async simulateTransaction(tx,options){simulations++;return isStonk?stonkSimulation(tx,input,options):
        {context:{slot:input.minSlot},value:{err:null,unitsConsumed:100000,accounts:[mintResponse(input)]}};}};
    const cache=new AccountCache(rpc,{commitment:'processed'});
    const common=load(isStonk?'stonk_native_accounts.json':'pump_native_accounts.json');
    for(const key of fast.shared(input)){
      const name=key.toBase58(),row=common.get(name);if(!row)continue;
      cache.record(name,row,input.minSlot-5,cache.generation,true);
      cache.watchState.set(name,{subscribed:true,version:1,dispose(){}});
    }
    cache.head=input.minSlot-1;cache.pulseAt=Date.now();
    const connection=cache.view(input.minSlot,false,{sharedKeys:fast.shared(input),
      blockhashCache:{pick(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};}}});
    try{
      // worker.cjs instruments every method with an async timing wrapper.
      const instrumented=new Proxy(connection,{get(target,key){const v=target[key];return typeof v==='function'?async(...a)=>v.apply(target,a):v;}});
      const result=await build(input,instrumented);
      assert.equal(result.localCreate.eligible,true);assert.equal(accountRPC,0);assert.equal(simulations,1);
      cache.head=input.minSlot-3;
      assert(connection.localCreateShared(fast.shared(input)).every(x=>x===null));
      cache.head=input.minSlot;cache.invalidate();
      assert(connection.localCreateShared(fast.shared(input)).every(x=>x===null));
    }finally{cache.close();}
  }
});

test('Stonk transfer verification accepts actual partially decoded RPC format',async()=>{
  const input=configured(sample('create_GcH')),common=load('stonk_native_accounts.json');
  const connection={async getMultipleAccountsInfo(keys){return keys.map(k=>common.get(String(k))||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){
      assert.equal(options.innerInstructions,true);
      const result=stonkSimulation(tx,input,options);
      for(const group of result.value.innerInstructions)group.instructions=group.instructions.map(ix=>{
        const i=ix.parsed.info,data=Buffer.alloc(10);data[0]=12;data.writeBigUInt64LE(BigInt(i.tokenAmount.amount),1);data[9]=6;
        return {programId:pk(ix.programId),accounts:[pk(i.source),pk(i.mint),pk(i.destination),pk(input.wallet)],data:require('bs58').encode(data)};
      });
      return result;
    }};
  assert.equal((await build(input,connection)).localCreate.eligible,true);
});


test('native Stonk verifier reproduces real create-buy receipt including platform fee transfer',()=>{
  const input=sample('create_GcH'),snapshot=input.createHint.localSnapshot;
  const raw=require('../fixtures/create_GcHEn2AHcGDc4Dbhx3bgVotqcrtnvmDwgtfSuT5ytCj.json');
  const {poolState,buyInstruction}=require('../../trade_execution/sdk/stonk.cjs');
  const {verify}=require('../../trade_execution/sdk/local-stonk.cjs');
  const mint=pk(input.mint),pool=pk(input.pool),user=pk(snapshot.user),baseProgram=pk(input.tokenProgram),quote=spl.NATIVE_MINT;
  const projected=local.project(input).rows,state=poolState(projected.get(input.pool),pool,mint,quote);
  const base=spl.unpackMint(mint,projected.get(input.mint),baseProgram),targetAta=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram);
  const nativeAta=spl.getAssociatedTokenAddressSync(quote,user),amount=5000000000n;
  const group=raw.meta.innerInstructions.find(g=>g.index===7);
  const gross=151640345927654n,minOut=gross-require('../../trade_execution/sdk/risk.cjs').tax(base,0,gross);
  const ix=buyInstruction({user,pool,mint,quote,baseProgram,quoteProgram:spl.TOKEN_PROGRAM_ID,state,targetAta,quoteAta:nativeAta,amount,minOut});
  const tx=new web3.VersionedTransaction(new web3.TransactionMessage({payerKey:user,recentBlockhash:web3.PublicKey.default.toBase58(),instructions:[ix]}).compileToV0Message());
  const value={innerInstructions:[{index:0,instructions:group.instructions}]};
  const args={mint,state,base,targetAta,nativeAta,amount,minOut,baseProgram};
  verify(value,tx,args);
  assert.throws(()=>verify(value,tx,{...args,minOut:minOut+1n}),/stonk-fill-rejected/);
});
