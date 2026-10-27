'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token');
const {build}=require('../../trade_execution/sdk/build.cjs');
const fast=require('../../trade_execution/sdk/create-fast.cjs');
const pk=x=>new web3.PublicKey(x),key=()=>web3.Keypair.generate().publicKey;
const rows=name=>Object.fromEntries(Object.entries(require('../fixtures/'+name).accounts).filter(([,v])=>v)
  .map(([k,v])=>[k,{...v,owner:pk(v.owner),data:Buffer.from(v.data[0],'base64')}]));
function hint(input,launchpad,config){return {version:1,slot:input.minSlot,mint:input.mint,
  quoteMint:input.quoteMint,pool:input.pool,launchpad,...(config?{config}:{})};}
test('Pump create skips ATA read, keeps exact budget/minOut and idempotent setup',async()=>{
  const a=require('../fixtures/pump_native_multi_buy.json').transaction.message.instructions[3].accounts;
  const values=rows('pump_native_accounts.json'),curve=values[a[3]].data;
  for(const [off,n] of [[8,1000000000000000n],[16,30000000000n],[24,800000000000000n],[32,5000000000n]])curve.writeBigUInt64LE(n,off);
  curve[48]=0;
  const user=key(),target=spl.getAssociatedTokenAddressSync(pk(a[2]),user,false,spl.TOKEN_PROGRAM_ID);
  const input={route:'pump_native_curve',wallet:user.toBase58(),mint:a[2],quoteMint:spl.NATIVE_MINT.toBase58(),
    tokenProgram:spl.TOKEN_PROGRAM_ID.toBase58(),pool:a[3],lookupTables:[],minSlot:100,amount:'10000000',slippagePercent:'2'};
  let queries=[],simulations=0,error=null;
  const connection={async getMultipleAccountsInfo(keys){queries.push(keys.map(k=>k.toBase58()));return keys.map(k=>values[k.toBase58()]||null);},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,c){simulations++;assert.equal(c.minContextSlot,100);return {context:{slot:100},value:{err:error,unitsConsumed:80000}};}};
  const normal=await build(input,connection);assert(queries[0].includes(target.toBase58()));queries=[];
  input.createHint=hint(input,'pump.fun');
  const quick=await build(input,connection);
  assert.equal(quick.minOut,normal.minOut);assert.equal(quick.quotedOut,normal.quotedOut);
  assert(!queries.flat().includes(target.toBase58()));assert.equal(simulations,2);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(quick.transaction,'base64'));
  const ata=web3.TransactionMessage.decompile(tx.message).instructions.find(ix=>ix.programId.equals(spl.ASSOCIATED_TOKEN_PROGRAM_ID));
  assert.deepEqual([...ata.data],[1]);assert.equal(tx.message.header.numRequiredSignatures,1);
  error={InstructionError:[2,'reject']};await assert.rejects(build(input,connection),/simulation-rejected/);
  for(const changed of [{slot:99},{mint:key().toBase58()},{pool:key().toBase58()},{launchpad:'unknown'}])
    assert.equal(fast.hint({...input,createHint:{...input.createHint,...changed}}),null);
});
test('Stonk create batches decoded config; still checks live pool binding, tax, net receipt and simulation',async()=>{
  const a=require('../fixtures/stonk_native_buy.json').transaction.message.instructions[6].accounts;
  const values=rows('stonk_native_accounts.json'),d=values[a[4]].data;d[17]=0;
  for(const [off,n] of [[29,793100000000000n],[37,1073025605596382n],[45,30000852951n],[53,138432646230590n],[61,4443750000n]])d.writeBigUInt64LE(n,off);
  const user=key();let queries=[],net=1000000000000n;
  const input={route:'stonk_native_curve',wallet:user.toBase58(),mint:a[9],quoteMint:a[10],tokenProgram:a[11],quoteProgram:a[12],
    pool:a[4],lookupTables:[],minSlot:100,amount:'10000000',slippagePercent:'2',minLiquidity:'0',risk:{totalFeeBps:600,impactBps:200}};
  const connection={async getMultipleAccountsInfo(keys){queries.push(keys.map(k=>k.toBase58()));return keys.map(k=>values[k.toBase58()]||null);},
    async getEpochInfo(){return {epoch:2000};},async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
    async simulateTransaction(tx,c){const data=Buffer.alloc(165);pk(a[9]).toBuffer().copy(data);user.toBuffer().copy(data,32);data.writeBigUInt64LE(net,64);data[108]=1;
      return {context:{slot:100},value:{err:null,unitsConsumed:150000,accounts:[{owner:a[11],data:[data.toString('base64'),'base64']}]}};}};
  const normal=await build(input,connection);assert(queries.some(q=>q.length===2&&q[0]===a[2]));queries=[];
  input.createHint=hint(input,'stonk',a[2]);const quick=await build(input,connection);
  assert.equal(quick.transaction,normal.transaction);assert.equal(queries[0].length,7);
  assert.equal(queries.filter(q=>q.includes(a[2])).length,1);assert.equal(queries.length,2);
  const bad={...input,createHint:{...input.createHint,config:key().toBase58()}};
  await assert.rejects(build(bad,connection),/stonk-pool-rejected/);
  net=1n;await assert.rejects(build(input,connection),/stonk-fill-rejected/);
  d[17]=2;await assert.rejects(build(input,connection),/stonk-pool-rejected/);
});

test('persistent worker uses create hints and returns per-method timing without external RPC', {timeout:15000},async t=>{
  const http=require('node:http'),readline=require('node:readline'),path=require('node:path');
  const {spawn}=require('node:child_process');
  const a=require('../fixtures/pump_native_multi_buy.json').transaction.message.instructions[3].accounts;
  const values=rows('pump_native_accounts.json'),curve=values[a[3]].data;
  for(const [off,n] of [[8,1000000000000000n],[16,30000000000n],[24,800000000000000n],[32,5000000000n]])curve.writeBigUInt64LE(n,off);
  curve[48]=0;const queries=[];
  const server=http.createServer((req,res)=>{let body='';req.on('data',b=>body+=b);req.on('end',()=>{
    const q=JSON.parse(body);queries.push(q);let result;
    if(q.method==='getMultipleAccounts')result={context:{slot:100},value:q.params[0].map(k=>{
      const v=values[k];return v?{...v,owner:v.owner.toBase58(),data:[v.data.toString('base64'),'base64']}:null;})};
    else if(q.method==='getLatestBlockhash')result={context:{slot:100},value:{blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900}};
    else if(q.method==='simulateTransaction')result={context:{slot:100},value:{err:null,logs:[],unitsConsumed:80000}};
    else {res.writeHead(400);res.end();return;}
    res.setHeader('content-type','application/json');res.end(JSON.stringify({jsonrpc:'2.0',id:q.id,result}));
  });});
  const wss=new(require('ws').Server)({server});
  wss.on('connection',socket=>socket.on('message',data=>{const q=JSON.parse(data);socket.send(JSON.stringify({jsonrpc:'2.0',id:q.id,result:q.id}));}));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const child=spawn(process.execPath,[path.resolve(__dirname,'../../trade_execution/sdk/worker.cjs')],{stdio:['pipe','pipe','ignore']});
  const exited=new Promise(resolve=>child.once('exit',resolve));
  t.after(async()=>{child.kill();await exited;for(const socket of wss.clients)socket.terminate();wss.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));});
  const user=key(),port=server.address().port;
  const input={operation:'build',route:'pump_native_curve',rpc:`http://127.0.0.1:${port}`,ws:`ws://127.0.0.1:${port}`,
    commitment:'processed',wallet:user.toBase58(),mint:a[2],quoteMint:spl.NATIVE_MINT.toBase58(),pool:a[3],
    tokenProgram:spl.TOKEN_PROGRAM_ID.toBase58(),lookupTables:[],minSlot:100,amount:'10000000',slippagePercent:'2'};
  input.createHint=hint(input,'pump.fun');
  const lines=readline.createInterface({input:child.stdout});
  const response=new Promise(resolve=>lines.on('line',line=>{const v=JSON.parse(line);if(v.id===1)resolve(v);}));
  child.stdin.write(JSON.stringify({id:1,input})+'\n');
  const row=await response;assert.equal(row.error,undefined);
  assert.equal(row.result.buildTiming.createFast,true);
  assert.equal(row.result.buildTiming.calls.simulateTransaction.count,1);
  assert(row.result.buildTiming.calls.getMultipleAccountsInfo.ms>=0);
  assert.equal(queries.filter(q=>q.method==='simulateTransaction').length,1);
  const read=queries.find(q=>q.method==='getMultipleAccounts');assert.equal(read.params[0].length,4);
});
