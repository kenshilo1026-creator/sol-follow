'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const {buildLocal,validateRecipe,recipeFrom}=require('../../trade_execution/sdk/local-routes.cjs');
const risk=require('../../trade_execution/sdk/risk.cjs');
const jupiter=require('../../trade_execution/sdk/jupiter.cjs');
const snapshot=require('../fixtures/local_quote_accounts.json');
const launch=require('../fixtures/stonk_route_accounts.json');
const ref=require('../fixtures/stonk_non_sol_buy.json');
const {build,safeMint}=require('../../trade_execution/sdk/build.cjs');
const pk=x=>new web3.PublicKey(x);
test('fee and impact caps are separate from minOut, including transfer tax',()=>{
  assert.throws(()=>risk.check({},[25000n],[0n]),/pool-fee-limit/);
  assert.throws(()=>risk.check({},[15000n,15000n,10000n],[0n],[15000n,15000n]),/total-fee-limit/);
  assert.throws(()=>risk.check({},[1000n],[15000n,15000n]),/price-impact-limit/);
  assert.doesNotThrow(()=>risk.check({},[500n,12500n,10000n],[1000n],[500n,12500n]));
  assert.throws(()=>risk.check({},[undefined],[0n]));
  assert.throws(()=>risk.check({risk:{poolFeeBps:-1,totalFeeBps:300,impactBps:200}},[0n],[0n]),/invalid-risk-data/);
});
test('route recipes reject split paths, cycles, wrong destination and unsupported venues',()=>{
  const quote=pk(snapshot.recipe.steps.at(-1).outputMint);
  validateRecipe(snapshot.recipe,quote);
  const base={routePlan:snapshot.recipe.steps.map(s=>({percent:100,swapInfo:{...s,ammKey:s.pool}})),addressesByLookupTableAddress:{}};
  recipeFrom(base,quote);
  base.routePlan[0].percent=50;assert.throws(()=>recipeFrom(base,quote),/cached-route-rejected/);
  for(const mutate of [r=>r.steps[0].outputMint=r.steps[0].inputMint,r=>r.steps[0].label='unknown',
    r=>r.steps.at(-1).outputMint=web3.Keypair.generate().publicKey.toBase58(),r=>r.steps[1].pool=r.steps[0].pool]){
    const r=structuredClone(snapshot.recipe);mutate(r);assert.throws(()=>validateRecipe(r,quote),/cached-route-rejected/);
  }
});
test('multi-hop only spends guaranteed previous output, and divides the slippage budget',async()=>{
  const spends=[],minimums=[];
  const result=await buildLocal({amount:1000000n,quoteMint:pk(snapshot.recipe.steps.at(-1).outputMint),slippageBps:100},snapshot.recipe,
    async()=>({setup:[],quote:async amount=>({out:amount*2n,fee:100n,impact:0n,instructions:async minimum=>{
      spends.push(amount);minimums.push(minimum);return [];}})}));
  assert.equal(result.expected,4000000n);assert.deepEqual(spends,[1000000n,...minimums.slice(0,-1)]);
  assert(result.minimum>=result.expected*99n/100n);
});
function fixtureConnection(user,fixture=snapshot){
  const rows=Object.fromEntries(Object.entries({...launch.accounts,...fixture.accounts}).filter(([,r])=>r).map(([k,r])=>
    [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));
  const a=ref.transaction.message.instructions[4].accounts;
  const d=rows[a[4]].data;d[17]=0;
  for(const [offset,n] of [[29,793100000000000n],[37,1073025605785265n],[45,405865756n],[53,335434497553928n],[61,184575674n]])d.writeBigUInt64LE(n,offset);
  let calls=0;
  const connection={
    async getAccountInfo(k){calls++;return rows[k.toBase58()]||null;},
    async getMultipleAccountsInfo(keys){calls++;return keys.map(k=>rows[k.toBase58()]||null);},
    async getMultipleAccountsInfoAndContext(keys){return {context:{slot:999999999},value:await this.getMultipleAccountsInfo(keys)};},
    async getEpochInfo(){return fixture.epoch||snapshot.epoch;},
    async getAddressLookupTable(k){return {value:new web3.AddressLookupTableAccount({key:k,state:web3.AddressLookupTableAccount.deserialize(rows[k.toBase58()].data)})};},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,options){
      const data=Buffer.alloc(165);pk(a[9]).toBuffer().copy(data);user.toBuffer().copy(data,32);data.writeBigUInt64LE(10000000000000n,64);data[108]=1;
      assert.equal(options.accounts.addresses.length,1);
      return {value:{err:null,unitsConsumed:500000,accounts:[{data:[data.toString('base64'),'base64'],owner:a[11],executable:false,lamports:2000000}]}};
    }};
  return {connection,rows,get calls(){return calls;}};
}
test('real Whirlpool snapshots build SOL-USDC-DJT locally with no Jupiter call',async t=>{
  t.mock.method(Date,'now',()=>snapshot.timestamp);
  const user=web3.Keypair.generate().publicKey,{connection,rows}=fixtureConnection(user);
  const quoteMint=pk(snapshot.recipe.steps.at(-1).outputMint);
  const args={user,quoteMint,amount:new BN(10000000),slippageBps:100,connection,safeMint,minLiquidity:'0',swapRecipe:snapshot.recipe};
  let api=0;
  const hop=await jupiter.buildHop(args,async()=>{api++;throw Error('must-not-call');});
  assert.equal(api,0);assert(hop.expected>0n);assert(hop.minimum>=hop.expected*99n/100n);
  assert.equal(hop.swaps.length,2);
  assert(hop.swaps.every(ix=>ix.programId.toBase58()==='whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'));
  assert.equal(hop.swaps[0].data.readBigUInt64LE(8),10000000n);
  for(let i=1;i<2;i++)assert.equal(hop.swaps[i].data.readBigUInt64LE(8),hop.swaps[i-1].data.readBigUInt64LE(16));
  const a=ref.transaction.message.instructions[4].accounts;
  const input={route:'sol_to_stonk_curve',wallet:user.toBase58(),mint:a[9],quoteMint:a[10],tokenProgram:a[11],quoteProgram:a[12],
    pool:a[4],lookupTables:[],minSlot:0,amount:'10000000',slippagePercent:'2',minLiquidity:'0',swapRecipe:snapshot.recipe};
  const result=await build(input,connection);
  const bytes=Buffer.from(result.transaction,'base64');assert(bytes.length<=1232);
  const tx=web3.VersionedTransaction.deserialize(bytes);assert(tx.signatures[0].every(n=>n===0));
  const tables=await Promise.all(snapshot.recipe.tables.map(async k=>(await connection.getAddressLookupTable(pk(k))).value));
  const ixs=web3.TransactionMessage.decompile(tx.message,{addressLookupTableAccounts:tables}).instructions;
  const buy=ixs.find(ix=>ix.programId.toBase58()===require('../../trade_execution/sdk/stonk.cjs').PROGRAM.toBase58());
  assert.equal(buy.data.readBigUInt64LE(16),BigInt(result.minOut));
  assert.equal(buy.data.readBigUInt64LE(8),hop.minimum);
  // Transfer tax alone pushes the combined fee over a deliberately tighter cap.
  await assert.rejects(build({...input,risk:{poolFeeBps:200,totalFeeBps:150,impactBps:200}},connection),/total-fee-limit/);
  // Real account owner validation still runs on a persisted recipe.
  rows[snapshot.recipe.steps[0].pool].owner=web3.SystemProgram.programId;
  await assert.rejects(jupiter.buildHop(args),/account-owner/);
});

test('real DLMM snapshot builds the direct cached SOL-DJT route and Stonk packet',async t=>{
  const fixture=require('../fixtures/local_dlmm_quote_accounts.json');
  t.mock.method(Date,'now',()=>fixture.timestamp);
  const user=web3.Keypair.generate().publicKey,{connection}=fixtureConnection(user,fixture);
  const a=ref.transaction.message.instructions[4].accounts;
  const input={route:'sol_to_stonk_curve',wallet:user.toBase58(),mint:a[9],quoteMint:a[10],tokenProgram:a[11],quoteProgram:a[12],
    pool:a[4],lookupTables:[],minSlot:0,amount:'10000000',slippagePercent:'2',minLiquidity:'0',swapRecipe:fixture.recipe};
  const result=await build(input,connection);
  assert(Buffer.from(result.transaction,'base64').length<=1232);
  assert(BigInt(result.quotedOut)>BigInt(result.minOut));
  assert.equal(result.quoteOut,fixture.expected);
  await assert.rejects(build({...input,risk:{poolFeeBps:0,totalFeeBps:300,impactBps:200}},connection),/pool-fee-limit/);
});


test('source cap values real non-SOL routes entirely from warmed snapshots',async t=>{
  const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
  const {quoteLimit}=require('../../trade_execution/sdk/observed-buy.cjs');
  let now=snapshot.timestamp;t.mock.method(Date,'now',()=>now);
  const user=web3.Keypair.generate().publicKey,{connection,rows}=fixtureConnection(user);
  const cache=new AccountCache(connection,{refreshMs:999999,commitment:'processed'});t.after(()=>cache.close());
  const slot=snapshot.epoch.absoluteSlot;
  for(const [k,row] of Object.entries(rows))cache.record(k,row,slot);
  cache.misc.set('epoch',{at:now,value:snapshot.epoch});
  const input={route:'sol_to_stonk_curve',wallet:user.toBase58(),quoteMint:snapshot.recipe.steps.at(-1).outputMint,
    limitAmount:'5000000000',minLiquidity:'0',swapRecipe:snapshot.recipe};
  // Warm missing tick-array sentinels, then make EVERY network function throw.
  const warm=await quoteLimit(input,cache.view(slot));
  for(const name of Object.keys(connection))if(typeof connection[name]==='function')connection[name]=async()=>{throw Error('network-forbidden');};
  const quoted=await quoteLimit(input,cache.view(slot,true));
  assert.equal(quoted.quoteLimit,warm.quoteLimit);assert(BigInt(quoted.quoteLimit)>0n);
  now+=2001;
  await assert.rejects(quoteLimit(input,cache.view(slot,true)),/price-cache-miss/);
});

test('foreground cold discovery never waits on paused background discovery',async()=>{
  let release,entered;
  const waiting=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const args={user:web3.Keypair.generate().publicKey,quoteMint:web3.Keypair.generate().publicKey,
    quoteAta:web3.Keypair.generate().publicKey,amount:1000n,slippageBps:200,connection:{},
    background:true,waitForBackground:async()=>{entered();await gate;}};
  const warm=jupiter.buildHop(args,async()=>{throw Error('fixture-end');});
  const completed=assert.rejects(warm,/jupiter-build-failed/);
  await waiting;
  try{
    await assert.rejects(jupiter.buildHop({...args,background:false}),/price-cache-miss/);
    await assert.rejects(jupiter.buildHop({...args,quoteMint:web3.Keypair.generate().publicKey,background:false}),/price-cache-miss/);
  }finally{release();await completed;}
});
