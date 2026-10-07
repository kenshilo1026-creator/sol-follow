const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const {marketCap,usdPrice,ORACLES,FEED,PYTH_DISC}=require('../../trade_execution/sdk/market-cap.cjs');
const pk=x=>new web3.PublicKey(x);
function oracle(){
 const data=Buffer.alloc(134);PYTH_DISC.copy(data);data[40]=1;FEED.copy(data,41);
 data.writeBigInt64LE(15000000000n,73);data.writeBigUInt64LE(1000000n,81);data.writeInt32LE(-8,89);data.writeBigInt64LE(1000n,93);
 return {data,owner:ORACLES[0].owner,executable:false};
}
function values(...sources){return Object.fromEntries(Object.entries(Object.assign({},...sources)).filter(([,v])=>v).map(([k,v])=>
 [k,{...v,owner:pk(v.owner),data:Buffer.from(v.data[0],'base64')}]));}
function connection(rows){rows[ORACLES[0].address.toBase58()]=oracle();return {
 calls:0,async getMultipleAccountsInfoAndContext(keys){this.calls++;return {context:{slot:100},value:keys.map(k=>rows[k.toBase58()]||null)};}
};}
test('Pyth owner, feed, verification, age, future timestamp and confidence are enforced',()=>{
 const row=oracle();assert.equal(usdPrice(row,ORACLES[0].owner,1000).value.toString(),'150');
 for(const mutate of [r=>r.owner=pk(web3.PublicKey.default),r=>r.data[40]=0,r=>r.data[41]^=1,
   r=>r.data.writeBigInt64LE(909n,93),r=>r.data.writeBigInt64LE(1006n,93),r=>r.data.writeBigUInt64LE(150000001n,81)]){
   const r=oracle();mutate(r);assert.throws(()=>usdPrice(r,ORACLES[0].owner,1000),/market-cap-price-unavailable/);
 }
});

test('native Pump FDV equals virtual quote/base times supply times USD, independent of trade size',async()=>{
 const rows=values(require('../fixtures/pump_native_accounts.json').accounts);
 const mint=Object.keys(rows).find(k=>rows[k].owner.equals(spl.TOKEN_PROGRAM_ID)&&rows[k].data.length===82);
 const curve=pump.bondingCurvePda(pk(mint)),d=rows[curve.toBase58()].data;
 d[48]=0;d.writeBigUInt64LE(1000000000000000n,8);d.writeBigUInt64LE(30000000000n,16);
 const rpc=connection(rows),input={route:'pump_native_curve',mint,quoteMint:spl.NATIVE_MINT.toBase58(),tokenProgram:spl.TOKEN_PROGRAM_ID.toBase58()};
 const result=await marketCap(input,rpc,1000);
 const supply=spl.unpackMint(pk(mint),rows[mint],spl.TOKEN_PROGRAM_ID).supply;
 assert.equal(result.marketCapUsdMicros,((supply*30000000000n*150n*1000000n+1000000000000000n*1000000000n-1n)/(1000000000000000n*1000000000n)).toString());
 assert.equal(rpc.calls,1);
});

for(const fixtureName of ['local_quote_accounts.json','local_dlmm_quote_accounts.json']){
 test('non-SOL Pump uses spot conversion from '+fixtureName+' with one account batch',async t=>{
  const ref=require('../fixtures/pump_non_sol_buy.json'),a=ref.transaction.message.instructions[7].accounts;
  const fixture=require('../fixtures/'+fixtureName),quote=fixture.recipe.steps.at(-1).outputMint;
  const rows=values(require('../fixtures/pump_route_accounts.json').accounts,fixture.accounts);
  const d=rows[a[10]].data,offset=d.indexOf(pk(a[2]).toBuffer());assert(offset>=0);pk(quote).toBuffer().copy(d,offset);
  d[48]=0;d.writeBigUInt64LE(1000000000000000n,8);d.writeBigUInt64LE(30000000000n,16);
  const rpc=connection(rows),input={route:'sol_to_pump_curve',mint:a[1],quoteMint:quote,tokenProgram:a[3],pool:a[10],swapRecipe:fixture.recipe};
  t.mock.method(globalThis,'fetch',async()=>{throw Error('network-forbidden');});
  const result=await marketCap(input,rpc,1000);
  assert(BigInt(result.marketCapUsdMicros)>0n);assert.equal(result.solUsd,'150.00000000');assert.equal(rpc.calls,1);
 });
}

test('native Stonk uses virtual plus/minus real reserves and Token-2022 supply',async()=>{
 const ref=require('../fixtures/stonk_native_buy.json'),a=ref.transaction.message.instructions[6].accounts;
 const rows=values(require('../fixtures/stonk_native_accounts.json').accounts),d=rows[a[4]].data;d[17]=0;
 for(const [offset,value] of [[37,1073025605596382n],[45,30000852951n],[53,138432646230590n],[61,4443750000n]])d.writeBigUInt64LE(value,offset);
 const result=await marketCap({route:'stonk_native_curve',mint:a[9],quoteMint:a[10],tokenProgram:a[11],pool:a[4]},connection(rows),1000);
 const supply=spl.unpackMint(pk(a[9]),rows[a[9]],pk(a[11])).supply;
 const numerator=supply*(30000852951n+4443750000n)*150n*1000000n,denominator=(1073025605596382n-138432646230590n)*1000000000n;
 assert.equal(result.marketCapUsdMicros,((numerator+denominator-1n)/denominator).toString());
});

for(const fixtureName of ['local_quote_accounts.json','local_dlmm_quote_accounts.json']){
 test('non-SOL Stonk converts quote spot to USD using '+fixtureName,async t=>{
  const ref=require('../fixtures/stonk_non_sol_buy.json'),a=ref.transaction.message.instructions[4].accounts;
  const fixture=require('../fixtures/'+fixtureName);
  const rows=values(require('../fixtures/stonk_route_accounts.json').accounts,fixture.accounts),d=rows[a[4]].data;
  d[17]=0;
  for(const [offset,value] of [[37,1073025605785265n],[45,405865756n],[53,335434497553928n],[61,184575674n]])d.writeBigUInt64LE(value,offset);
  const rpc=connection(rows),input={route:'sol_to_stonk_curve',mint:a[9],quoteMint:a[10],tokenProgram:a[11],pool:a[4],swapRecipe:fixture.recipe};
  t.mock.method(globalThis,'fetch',async()=>{throw Error('network-forbidden');});
  const first=await marketCap(input,rpc,1000);
  assert(BigInt(first.marketCapUsdMicros)>0n);assert.equal(rpc.calls,1);
  // Doubling supply must double FDV, without using a trade-size quote.
  const supply=rows[a[9]].data.readBigUInt64LE(36);rows[a[9]].data.writeBigUInt64LE(supply*2n,36);
  const second=await marketCap(input,rpc,1000);
  const diff=BigInt(second.marketCapUsdMicros)-2n*BigInt(first.marketCapUsdMicros);
  assert(diff>=-1n&&diff<=0n);
  await assert.rejects(marketCap({...input,swapRecipe:null},rpc,1000),/cached-route-rejected/);
  rows[fixture.recipe.steps[0].pool].owner=web3.SystemProgram.programId;
  await assert.rejects(marketCap(input,rpc,1000),/account-owner/);
 });
}
