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
    const evidence=quick.buildDiagnostics;
    assert(evidence.local_create.eligible);
    assert.deepEqual(evidence.reads.find(r=>r.phase==='projection').accounts.map(a=>a.source),
      ['source-events','source-events','cache-read','cache-read']);
    assert.equal(evidence.quote.min_out,quick.minOut);
    assert.equal(evidence.quote.virtual_token_reserves,pump.PUMP_SDK.decodeBondingCurve(projected.get(input.pool)).virtualTokenReserves.toString());
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
    cache.head=input.minSlot-20;cache.pulseAt=Date.now();
    const connection=cache.view(input.minSlot,false,{sharedKeys:fast.shared(input),
      blockhashCache:{pick(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};}}});
    try{
      // worker.cjs instruments every method with an async timing wrapper.
      const instrumented=new Proxy(connection,{get(target,key){const v=target[key];return typeof v==='function'?async(...a)=>v.apply(target,a):v;}});
      const result=await build(input,instrumented);
      assert.equal(result.localCreate.eligible,true);assert.equal(accountRPC,0);assert.equal(simulations,1);
      cache.head=input.minSlot-30;
      assert(connection.localCreateShared(fast.shared(input)).some(Boolean));
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


test('Pump build survives socket loss during shared HTTP read and still simulates at source slot',async t=>{
  const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
  const {EventEmitter}=require('node:events');
  const input=configured(sample('create_CBL')),common=load('pump_native_accounts.json');
  const socket=new EventEmitter();let reads=0,simulations=0;
  const rpc={_rpcWebSocket:socket,
    async getMultipleAccountsInfoAndContext(keys,config){
      reads++;assert.equal(config.minContextSlot,input.minSlot);
      assert.deepEqual(keys.map(String),[pump.GLOBAL_PDA.toBase58(),pump.PUMP_FEE_CONFIG_PDA.toBase58()]);
      socket.emit('close');
      return {context:{slot:input.minSlot},value:keys.map(k=>common.get(String(k)))};
    },
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){
      simulations++;assert.equal(options.minContextSlot,input.minSlot);
      return {context:{slot:input.minSlot},value:{err:null,unitsConsumed:100000,
        accounts:options.accounts?[mintResponse(input)]:undefined}};
    }};
  const cache=new AccountCache(rpc,{commitment:'processed'});t.after(()=>cache.close());
  const evidence=[],connection=cache.view(input.minSlot,false,{trace:r=>evidence.push(r)});
  const result=await build(input,connection);
  assert.equal(reads,1);assert.equal(simulations,1);assert(BigInt(result.minOut)>0n);
  assert.equal(result.priorityFee.chargedLamports,'200000');
  assert.equal(cache.rows.size,0);assert.equal(cache.executionGeneration,0);
  assert(evidence.some(r=>r.continued_after_disconnect));
});

test('last-known-good settings build Pump and Stonk after socket loss with ZERO account RPC reads',async()=>{
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
    cache.keepSettings(fast.settings(input));
    const common=load(isStonk?'stonk_native_accounts.json':'pump_native_accounts.json');
    for(const key of fast.shared(input)){
      const name=key.toBase58(),row=common.get(name);if(!row)continue;
      cache.record(name,row,input.minSlot-5,cache.generation,true);
      cache.watchState.set(name,{subscribed:true,version:1,dispose(){}});
    }
    cache.invalidate({reason:'websocket-close'});
    for(const row of cache.lastSettings.values())row.at-=60000;
    rpc._rpcWebSocketConnected=false;
    const evidence=[];
    const connection=cache.view(input.minSlot,false,{sharedKeys:fast.shared(input),trace:r=>evidence.push(r),
      blockhashCache:{pick(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};}}});
    try{
      // worker.cjs instruments every method with an async timing wrapper.
      const instrumented=new Proxy(connection,{get(target,key){const v=target[key];return typeof v==='function'?async(...a)=>v.apply(target,a):v;}});
      const result=await build(input,instrumented);
      assert.equal(result.localCreate.eligible,true);assert.equal(accountRPC,0);assert.equal(simulations,1);
      assert(BigInt(result.minOut)>0n);
      const hits=evidence.flatMap(r=>r.accounts||[]).filter(a=>a.last_known_good);
      assert(hits.length>=2);assert(hits.every(a=>a.reason==='last-known-good'&&a.age_ms>=60000));
      cache.head=input.minSlot-30;
      assert(connection.localCreateShared(fast.shared(input)).some(Boolean));
      cache.head=input.minSlot;cache.invalidate();
      assert(connection.localCreateShared(fast.shared(input)).every(x=>x===null));
    }finally{cache.close();}
  }
});


const v3Input=()=>structuredClone(require('../fixtures/pump_v3_create_input.json'));
const eventCodec=require('../../trade_execution/sdk/pump-create-events.cjs');
test('real V3 create uses post-trade reserves excluding rent and retained fees',()=>{
  const input=v3Input(),projection=local.project(input);
  assert.equal(projection.rows.size,2);
  const state=pump.PUMP_SDK.decodeBondingCurve(projection.rows.get(input.pool));
  assert.equal(state.virtualTokenReserves.toString(),'1059053615030649');
  assert.equal(state.virtualQuoteReserves.toString(),'30395061727');
  assert.equal(state.realTokenReserves.toString(),'779153615030649');
  assert.equal(state.realQuoteReserves.toString(),'395061727');
  const e=eventCodec.trade(Buffer.from(input.createHint.localSnapshot.events[1],'base64'));
  assert.equal(e.opaqueZeroTail,true);assert.equal(e.creatorFeeUnclaimed,1185186n);
  const retained=BigInt(e.fee.toString())-BigInt(e.buybackFee.toString())+BigInt(e.creatorFee.toString());
  assert.equal(retained,3061730n);
  assert.equal(395061727n+retained+1493520n,399616977n);
});

test('V3 unknown tail, missing proof, mismatched balances and incomplete events fall back',()=>{
  for(const mutate of [
    s=>delete s.receiptEvidence,
    s=>s.receiptEvidence.poolTokens='986053615030648',
    s=>s.receiptEvidence.poolLamports='399616978',
    s=>s.receiptEvidence.rentLamports='0',
    s=>s.eventInstructions[1]='SellV3',
    s=>s.tradeCount++,
    s=>{const b=Buffer.from(s.events[1],'base64');b[b.length-1]=1;s.events[1]=b.toString('base64');},
    s=>s.events[1]=Buffer.from(s.events[1],'base64').subarray(0,-1).toString('base64'),
    s=>s.events.push(Buffer.alloc(20).toString('base64')),
    s=>{const b=Buffer.from(s.events[1],'base64');b.writeBigUInt64LE(b.readBigUInt64LE(113)+1n,113);s.events[1]=b.toString('base64');},
  ]){const input=v3Input();mutate(input.createHint.localSnapshot);assert.equal(local.project(input).rows.size,0);}
});

test('published V3 tail and depth decode, sequential buy/sell reserve transitions stay fee-neutral',()=>{
  const input=v3Input(),s=input.createHint.localSnapshot;
  const c=Buffer.from(s.events[0],'base64'),t=Buffer.from(s.events[1],'base64');
  const published=t.subarray(0,-8),event=eventCodec.trade(published);
  assert.equal(event.opaqueZeroTail,false);assert.equal(event.creatorFeeUnclaimed,1185186n);
  const depth1=Buffer.from(c);depth1[depth1.length-1]=1;
  assert.equal(eventCodec.create(depth1).depth,1);
  assert.throws(()=>eventCodec.create(Buffer.concat([c,Buffer.from([3])])));
  assert.throws(()=>eventCodec.create(Buffer.concat([c,Buffer.alloc(2)])));
  const initial={vt:1000n,vq:100n,rt:500n,rq:0n};
  const buy={isBuy:true,tokenAmount:100n,quoteAmount:12n,virtualTokenReserves:900n,
    virtualQuoteReserves:112n,realTokenReserves:400n,realQuoteReserves:12n};
  const after=eventCodec.advance(initial,buy);
  const sell={isBuy:false,tokenAmount:50n,quoteAmount:5n,virtualTokenReserves:950n,
    virtualQuoteReserves:107n,realTokenReserves:450n,realQuoteReserves:7n};
  assert.deepEqual(eventCodec.advance(after,sell),{vt:950n,vq:107n,rt:450n,rq:7n});
  assert.throws(()=>eventCodec.advance(initial,{...buy,realTokenReserves:0n}));
  assert.throws(()=>eventCodec.advance(initial,{...buy,virtualQuoteReserves:113n}));
});

test('real V3 source builds with warm settings and ZERO account RPC calls, even after socket loss',async()=>{
  const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
  const fast=require('../../trade_execution/sdk/create-fast.cjs');
  for(const disconnected of [false,true]){
    const input=configured(v3Input());let accountRPC=0,simulations=0;
    const rpc={_rpcWebSocketConnected:true,
      async getMultipleAccountsInfoAndContext(){accountRPC++;throw Error('unexpected account RPC');},
      async simulateTransaction(tx,options){
        simulations++;assert.equal(options.minContextSlot,input.minSlot);
        return {context:{slot:input.minSlot},value:{err:null,unitsConsumed:100000,accounts:[mintResponse(input)]}};
      }};
    const cache=new AccountCache(rpc,{commitment:'processed'}),common=load('pump_native_accounts.json');
    cache.keepSettings(fast.settings(input));
    for(const key of fast.shared(input)){
      const name=String(key),value=common.get(name);if(!value)continue;
      cache.record(name,value,input.minSlot-5,cache.generation,true);
      cache.watchState.set(name,{subscribed:true,version:1,dispose(){}});
    }
    if(disconnected){cache.invalidate({reason:'websocket-close'});rpc._rpcWebSocketConnected=false;}
    const conn=cache.view(input.minSlot,false,{sharedKeys:fast.shared(input),
      blockhashCache:{pick(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};}}});
    try{
      const built=await build(input,conn);
      assert.equal(built.localCreate.eligible,true);assert.deepEqual(built.localCreate.projectedAccounts,[input.mint,input.pool]);
      assert.equal(accountRPC,0);assert.equal(simulations,1);assert(BigInt(built.minOut)>0n);
      assert.equal(built.priorityFee.chargedLamports,'200000');
      assert.equal(built.buildDiagnostics.quote.real_quote_reserves,'395061727');
    }finally{cache.close();}
  }
});


test('non-SOL V3 source keeps quote units and validates retained quote-token balance',()=>{
  const input=v3Input(),s=input.createHint.localSnapshot,quote=web3.Keypair.generate().publicKey;
  const c=eventCodec.create(Buffer.from(s.events[0],'base64')),t=eventCodec.trade(Buffer.from(s.events[1],'base64'));
  c.quoteMint=quote;t.quoteMint=quote;
  const encode=(name,event,old,tail)=>Buffer.concat([Buffer.from(old,'base64').subarray(0,8),
    pump.PUMP_SDK.offlinePumpProgram.coder.types.encode(name,event),tail]).toString('base64');
  s.events=[encode('createEvent',c,s.events[0],Buffer.from([1])),
    encode('tradeEvent',t,s.events[1],Buffer.from(s.events[1],'base64').subarray(-16))];
  input.route='sol_to_pump_curve';input.quoteMint=input.createHint.quoteMint=s.quoteMint=String(quote);
  s.receiptEvidence={poolTokens:'986053615030649',quoteTokens:'398123457'};
  const projection=local.project(input);assert.equal(projection.rows.size,2);
  const state=pump.PUMP_SDK.decodeBondingCurve(projection.rows.get(input.pool));
  assert.equal(String(state.quoteMint),String(quote));assert.equal(state.realQuoteReserves.toString(),'395061727');
  s.receiptEvidence.quoteTokens='395061727';assert.equal(local.project(input).rows.size,0);
});


const legacyModernInput=()=>structuredClone(require('../fixtures/pump_legacy_buy_modern_event_input.json'));
test('real BuyV2 with extended event tail requires receipt proof and projects final reserves',()=>{
  const input=legacyModernInput(),snapshot=input.createHint.localSnapshot;
  assert.deepEqual(snapshot.eventInstructions,['CreateV2','BuyV2']);
  const event=eventCodec.trade(Buffer.from(snapshot.events[1],'base64'));
  assert.equal(event.opaqueZeroTail,true);
  const projection=local.project(input);assert.equal(projection.rows.size,2);
  const state=pump.PUMP_SDK.decodeBondingCurve(projection.rows.get(input.pool));
  for(const key of ['virtualTokenReserves','virtualQuoteReserves','realTokenReserves','realQuoteReserves'])
    assert.equal(state[key].toString(),event[key].toString());
  // BuyV2 pays fees out; V3 retained-fee accounting must not be applied to it.
  const proof=snapshot.receiptEvidence;
  assert.equal(BigInt(proof.poolLamports)-BigInt(proof.rentLamports),BigInt(event.realQuoteReserves.toString()));
  for(const mutate of [s=>delete s.receiptEvidence,s=>delete s.eventInstructions,
    s=>s.receiptEvidence.poolLamports='45706762',s=>s.receiptEvidence.poolTokens='998420966890422',
    s=>s.eventInstructions[1]='BuyV3',
    s=>{const b=Buffer.from(s.events[1],'base64');b[b.length-1]=1;s.events[1]=b.toString('base64');}]){
    const bad=legacyModernInput();mutate(bad.createHint.localSnapshot);assert.equal(local.project(bad).rows.size,0);
  }
});

test('real BuyV2 extended-event build uses warm settings with zero account RPC calls',async()=>{
  const input=configured(legacyModernInput()),common=load('pump_native_accounts.json');
  let simulations=0,reads=0;
  const connection={
    async localCreateShared(keys){return keys.map(key=>common.get(String(key))||null);},
    async getMultipleAccountsInfo(){reads++;throw Error('unexpected account RPC');},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){simulations++;assert.equal(options.minContextSlot,input.minSlot);
      return {context:{slot:input.minSlot},value:{err:null,unitsConsumed:100000,accounts:[mintResponse(input)]}};}
  };
  const built=await build(input,connection);
  assert.equal(built.localCreate.eligible,true);assert.equal(reads,0);assert.equal(simulations,1);
  assert.deepEqual(built.localCreate.projectedAccounts,[input.mint,input.pool]);
  assert(BigInt(built.minOut)>0n);
});
