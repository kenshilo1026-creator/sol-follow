'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const web3=require('@solana/web3.js');
const spl=require('@solana/spl-token');
const {build}=require('./build.cjs');
const stonk=require('./stonk.cjs');
const jupiter=require('./jupiter.cjs');
const ref=require('../../tests/fixtures/stonk_non_sol_buy.json');
const snapshot=require('../../tests/fixtures/stonk_route_accounts.json');
const a=ref.transaction.message.instructions[4].accounts;
const pk=s=>new web3.PublicKey(s);
function values(){return Object.fromEntries(Object.entries(snapshot.accounts).filter(([,r])=>r).map(([k,r])=>
  [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));}
function active(rows){
  // Restore the pre-buy reserves recorded in the historical trade event. This
  // is an offline historical reconstruction, NOT the current graduated pool.
  const d=rows[a[4]].data;d[17]=0;
  for(const [offset,value] of [[29,793100000000000n],[37,1073025605785265n],[45,405865756n],
    [53,335434497553928n],[61,184575674n]])d.writeBigUInt64LE(value,offset);
}
test('historical LaunchLab quote reproduces exact transfer-tax net receipt',()=>{
  const r=values();active(r);
  const state=stonk.poolState(r[a[4]],pk(a[4]),pk(a[9]),pk(a[10]));
  const rate=stonk.feeSchedule(r[a[2]],r[a[3]],pk(a[10]));
  assert.equal(rate,12500n);
  const mint=spl.unpackMint(pk(a[9]),r[a[9]],spl.TOKEN_2022_PROGRAM_ID);
  assert.equal(stonk.quoteNet(state,4641142n,rate,mint,2000),5624421625112n);
  r[a[4]].data[17]=2;
  assert.throws(()=>stonk.poolState(r[a[4]],pk(a[4]),pk(a[9]),pk(a[10])),/stonk-pool-rejected/);
});
test('new LaunchLab metas match reference, no sample tips, nonce or referral fee',()=>{
  const r=values();active(r);
  const state=stonk.poolState(r[a[4]],pk(a[4]),pk(a[9]),pk(a[10]));
  const ix=stonk.buyInstruction({user:pk(a[0]),pool:pk(a[4]),mint:pk(a[9]),quote:pk(a[10]),
    baseProgram:pk(a[11]),quoteProgram:pk(a[12]),state,targetAta:pk(a[5]),quoteAta:pk(a[6]),amount:77n,minOut:55n});
  assert.deepEqual(ix.keys.map(k=>k.pubkey.toBase58()),a);
  assert.equal(ix.data.readBigUInt64LE(8),77n);
  assert.equal(ix.data.readBigUInt64LE(16),55n);
  assert.equal(ix.data.readBigUInt64LE(24),0n);
});
function response(args){
  return {inputMint:spl.NATIVE_MINT.toBase58(),outputMint:args.quoteMint.toBase58(),inAmount:args.amount.toString(),
    outAmount:'1000000',otherAmountThreshold:'990000',slippageBps:100,swapMode:'ExactIn',
    platformFee:null,routePlan:[{swapInfo:{label:'Whirlpool'}}],setupInstructions:[],
    swapInstruction:{programId:'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4',
      data:require('node:crypto').createHash('sha256').update('global:route').digest().subarray(0,8).toString('base64'),
      accounts:[{pubkey:args.user.toBase58(),isSigner:true,isWritable:false},{pubkey:args.quoteAta.toBase58(),isSigner:false,isWritable:true}]},
    cleanupInstruction:null,otherInstructions:[],addressesByLookupTableAddress:{}};
}
test('Jupiter first leg constrained to configured SOL budget and supported venues',async()=>{
  const args={user:pk(a[0]),quoteMint:pk(a[10]),quoteAta:pk(a[6]),amount:10000000n,slippageBps:100};
  assert.equal(jupiter.validate(response(args),args).minimum,990000n);
  for(const mutate of [b=>b.inAmount='20000000',b=>b.swapMode='ExactOut',
    b=>b.routePlan[0].swapInfo.label='unapproved-venue',b=>b.tipInstruction={},
    b=>b.cleanupInstruction={},b=>b.otherInstructions=[{}],b=>b.otherAmountThreshold='1',
    b=>b.platformFee={feeBps:1},b=>b.swapInstruction.programId='11111111111111111111111111111111']){
    const b=response(args);mutate(b);assert.throws(()=>jupiter.validate(b,args),/jupiter-route-rejected/);
  }
  let request;
  await jupiter.buildHop(args,async(url,options)=>{
    request={url,options};return {ok:true,json:async()=>response(args)};
  });
  assert.equal(request.options.headers,undefined);
  assert.equal(new URL(request.url).searchParams.get('wrapAndUnwrapSol'),'false');
  await assert.rejects(jupiter.buildHop(args,async()=>{throw Error('provider-secret');}),
    e=>e.message==='jupiter-build-failed');
});

test('keyless throttling propagates Retry-After without retrying a stale quote',async t=>{
  const args={user:pk(a[0]),quoteMint:pk(a[10]),quoteAta:pk(a[6]),amount:10000000n,slippageBps:100};
  t.mock.method(Date,'now',()=>Date.UTC(2026,9,6));
  for(const [header,expected] of [['30',30],[null,60],['invalid',60],['0',2],
    ['Tue, 06 Oct 2026 00:00:45 GMT',45]]){
    let calls=0;
    await assert.rejects(jupiter.buildHop(args,async()=>{
      calls++;return {ok:false,status:429,headers:{get:()=>header}};
    }),e=>e.message==='jupiter-rate-limited'&&e.retryAfter===expected);
    assert.equal(calls,1);
  }
});
test('full SOL-to-Stonk build composes atomic swap, net minimum and public RPC simulation',async t=>{
  const r=values();active(r);const user=web3.Keypair.generate().publicKey;
  const target=spl.getAssociatedTokenAddressSync(pk(a[9]),user,false,spl.TOKEN_2022_PROGRAM_ID);
  const quoteAta=spl.getAssociatedTokenAddressSync(pk(a[10]),user,false,spl.TOKEN_2022_PROGRAM_ID);
  let simulated,net=10000000000000n,simError=null,existingQuote=0n,consumeQuote=false;
  function tokenRow(mint,amount){
    const data=Buffer.alloc(165);mint.toBuffer().copy(data);user.toBuffer().copy(data,32);
    data.writeBigUInt64LE(amount,64);data[108]=1;
    return {owner:spl.TOKEN_2022_PROGRAM_ID,data,executable:false,lamports:2000000};
  }
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>existingQuote&&k.equals(quoteAta)?tokenRow(pk(a[10]),existingQuote):r[k.toBase58()]||null);},
    async getEpochInfo(){return {epoch:2000};},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){
      simulated=tx;assert.deepEqual(options.accounts.addresses,[target.toBase58(),...(existingQuote?[quoteAta.toBase58()]:[])]);
      const accounts=[tokenRow(pk(a[9]),net),...(existingQuote?[tokenRow(pk(a[10]),existingQuote-(consumeQuote?1n:0n))]:[])];
      return {value:{err:simError,unitsConsumed:220000,accounts:accounts.map(row=>
        ({...row,owner:row.owner.toBase58(),data:[row.data.toString('base64'),'base64']}))}};
    },
  };
  t.mock.method(jupiter,'buildHop',async(args)=>{
    assert.equal(args.amount.toString(),'10000000');
    return {...jupiter.validate(response(args),args),fees:[500n],impacts:[0n]};
  });
  const input={route:'sol_to_stonk_curve',wallet:user.toBase58(),mint:a[9],quoteMint:a[10],
    tokenProgram:a[11],quoteProgram:a[12],pool:a[4],lookupTables:[],minSlot:0,
    amount:'10000000',slippagePercent:'2',minLiquidity:'0'};
  const result=await build(input,connection);
  assert.equal(result.amount,'10000000');assert.equal(result.quoteIn,'990000');
  assert(BigInt(result.minOut)<BigInt(result.quotedOut));
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  assert(tx.signatures[0].every(x=>x===0));assert.equal(tx.message.header.numRequiredSignatures,1);
  const msg=web3.TransactionMessage.decompile(tx.message);
  const buy=msg.instructions.find(ix=>ix.programId.equals(stonk.PROGRAM));
  assert.equal(buy.data.readBigUInt64LE(16),BigInt(result.minOut));
  assert.equal(buy.data.readBigUInt64LE(8),990000n);
  const system=msg.instructions.filter(ix=>ix.programId.equals(web3.SystemProgram.programId));
  assert.equal(system.length,1);assert.equal(system[0].data.readBigUInt64LE(4),10000000n);
  net=1n;await assert.rejects(build(input,connection),/stonk-fill-rejected/);
  net=10000000000000n;simError={InstructionError:[5,'test']};
  await assert.rejects(build(input,connection),/simulation-rejected/);
  simError=null;existingQuote=100n;await build(input,connection);
  consumeQuote=true;await assert.rejects(build(input,connection),/stonk-fill-rejected/);
});
