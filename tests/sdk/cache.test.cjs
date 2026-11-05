'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const web3=require('@solana/web3.js');
const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
const key=()=>web3.Keypair.generate().publicKey;
function rpc(){
  return {_rpcWebSocket:new EventEmitter(),calls:0,slot:100,callbacks:new Map(),removed:[],
    async getMultipleAccountsInfoAndContext(keys,config){this.calls++;this.config=config;return {context:{slot:this.slot},value:keys.map(()=>({data:Buffer.from('state')}))};},
    onAccountChange(k,cb){const id=this.callbacks.size;this.callbacks.set(id,cb);return id;},
    async removeAccountChangeListener(id){this.removed.push(id);},
    async simulateTransaction(tx,config){return config;},async getLatestBlockhash(config){return config;}};
}
test('cache only hits within TTL and at/after signal slot; no stale fallback',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=rpc(),cache=new AccountCache(r,{ttlMs:1000,maxAccounts:3,refreshMs:999999});t.after(()=>cache.close());
  const k=key(),view=cache.view(100);
  await view.getAccountInfo(k);await view.getAccountInfo(k);assert.equal(r.calls,1);
  r.slot=101;await cache.view(101).getAccountInfo(k);assert.equal(r.calls,2);
  now+=1001;r.slot=102;await view.getAccountInfo(k);assert.equal(r.calls,3);
  await assert.rejects(cache.view(103).getAccountInfo(k),/cache-slot-behind/);
  assert.equal((await cache.view(200).simulateTransaction({},{})).minContextSlot,200);
  assert.equal((await cache.view(200).getLatestBlockhash()).minContextSlot,200);
  r.getMultipleAccountsInfoAndContext=async()=>{throw Error('offline');};now+=1001;
  await assert.rejects(view.getAccountInfo(k),/offline/);
});
test('websocket refresh, eviction, reconnect invalidation and in-flight disconnect',async t=>{
  const r=rpc(),cache=new AccountCache(r,{maxAccounts:2,refreshMs:999999});t.after(()=>cache.close());
  const a=key(),b=key(),c=key();await cache.view().getAccountInfo(a);
  r.callbacks.get(0)({data:Buffer.from('new')},{slot:150});
  assert.equal((await cache.view(150).getAccountInfo(a)).data.toString(),'new');assert.equal(r.calls,1);
  await cache.view().getAccountInfo(b);await cache.view().getAccountInfo(c);
  assert.equal(cache.rows.size,2);assert.equal(cache.subscriptions.size,2);assert.deepEqual(r.removed,[0]);
  r._rpcWebSocket.emit('close');assert.equal(cache.rows.size,0);
  await cache.view().getAccountInfo(key());await cache.view().getAccountInfo(key());
  assert(cache.subscriptions.size<=2);
  r.getMultipleAccountsInfoAndContext=async()=>{r._rpcWebSocket.emit('close');return {context:{slot:200},value:[{}]};};
  assert.deepEqual(await cache.view(200).getAccountInfo(a),{});assert.equal(cache.rows.size,0);
});
test('real JSON-lines worker shares state across requests and enforces a newer slot', {timeout:15000},async t=>{
  const http=require('node:http'),{spawn}=require('node:child_process'),readline=require('node:readline'),path=require('node:path');
  let calls=0;
  const server=http.createServer((req,res)=>{
    let body='';req.on('data',c=>body+=c);req.on('end',()=>{
      const request=JSON.parse(body);
      // Core-settings recovery now also uses this server. Count the target
      // account separately to verify foreground cache hits still avoid RPC.
      if(request.params[0].includes(input.primeAccounts[0]))calls++;
      const slot=Math.max(100,request.params[1].minContextSlot||0);
      res.setHeader('content-type','application/json');res.end(JSON.stringify({jsonrpc:'2.0',id:request.id,
        result:{context:{slot},value:request.params[0].map(()=>null)}}));
    });
  });
  const wss=new (require('ws').Server)({server});
  wss.on('connection',socket=>socket.on('message',data=>{const req=JSON.parse(data);socket.send(JSON.stringify({jsonrpc:'2.0',id:req.id,result:req.id}));}));
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const child=spawn(process.execPath,[path.resolve(__dirname,'../../trade_execution/sdk/worker.cjs')],{stdio:['pipe','pipe','ignore']});
  const exited=new Promise(r=>child.once('exit',r));
  t.after(async()=>{child.kill();await exited;for(const socket of wss.clients)socket.terminate();wss.close();server.closeAllConnections();await new Promise(r=>server.close(r));});
  const lines=readline.createInterface({input:child.stdout});
  const pending=new Map();lines.on('line',line=>{const r=JSON.parse(line);if(r.result||r.error)pending.get(r.id)?.(r);});
  const input={route:'prime',rpc:`http://127.0.0.1:${server.address().port}`,ws:`ws://127.0.0.1:${server.address().port}`,minSlot:100,lookupTables:[],
    primeAccounts:[key().toBase58()],cache:{ttlMs:2000,refreshMs:999999,maxAccounts:32}};
  async function request(id,minSlot,extra={}){const response=new Promise(r=>pending.set(id,r));child.stdin.write(JSON.stringify({id,input:{...input,minSlot,...extra}})+'\n');return response;}
  assert.equal((await request(1,100)).result.warmed,true);assert.equal(calls,1);
  // Starting a WS subscription may invalidate the pre-ACK HTTP snapshot.
  // Establish a stable cached read before asserting reuse across requests.
  let warmed;
  for(let id=10;id<15;id++){warmed=await request(id,100);if(warmed.stats.hits>0)break;}
  assert(warmed.stats.hits>0);const initialCalls=calls,initialHits=warmed.stats.hits;
  assert.equal((await request(2,100)).stats.hits,initialHits+1);assert.equal(calls,initialCalls);
  assert.equal((await request(3,101)).result.warmed,true);assert.equal(calls,initialCalls+1);
  const fixture=require('../fixtures/local_quote_accounts.json');
  const result=await request(4,101,{operation:'quote_minimum',minimumUsdMicros:'50000000',
    route:'sol_to_stonk_curve',quoteMint:fixture.recipe.steps.at(-1).outputMint,
    wallet:key().toBase58(),swapRecipe:fixture.recipe,minLiquidity:'0'});
  assert.equal(result.error,'price-cache-miss');assert.equal(calls,initialCalls+1); // no HTTP on minimum decisions
  const maximum=await request(5,101,{operation:'quote_limit',maximumUsdMicros:'500000000',
    route:'sol_to_stonk_curve',quoteMint:fixture.recipe.steps.at(-1).outputMint,
    wallet:key().toBase58(),swapRecipe:fixture.recipe,minLiquidity:'0'});
  assert.equal(maximum.error,'price-cache-miss');assert.equal(calls,initialCalls+1);
});


test('processed cache-only decisions never fall back to network, even for SDK methods',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=rpc(),cache=new AccountCache(r,{ttlMs:1000,refreshMs:999999,commitment:'processed'});t.after(()=>cache.close());
  const k=key();await cache.view(100).getAccountInfo(k);
  assert.equal(r.config.commitment,'processed');
  const view=cache.view(100,true);assert((await view.getAccountInfo(k)).data);
  assert.equal(r.calls,1);
  await assert.rejects(view.getAccountInfo(key()),/price-cache-miss/);
  await assert.rejects(cache.view(101,true).getAccountInfo(k),/price-cache-miss/);
  assert.throws(()=>view.getLatestBlockhash(),/price-cache-miss/);
  assert.throws(()=>view.simulateTransaction({},{}),/price-cache-miss/);
  await assert.rejects(view.getEpochInfo(),/price-cache-miss/);
  assert.equal(r.calls,1);
  now+=1001;await assert.rejects(view.getAccountInfo(k),/price-cache-miss/);
  cache.invalidate();assert.equal(cache.rows.size,0);
});

test('oversized batches retain valid hits even when subscriptions evict those cache rows',async t=>{
  const r=rpc(),cache=new AccountCache(r,{maxAccounts:2});t.after(()=>cache.close());
  const a=key(),b=key(),c=key();await cache.view().getAccountInfo(a);
  const rows=await cache.view().getMultipleAccountsInfo([a,b,c]);
  assert.equal(rows.length,3);assert(rows.every(row=>row.data.toString()==='state'));
  assert.equal(r.calls,2);assert(cache.rows.size<=2);assert(cache.subscriptions.size<=2);
});


test('account diagnostics report misses, actual HTTP keys, then cache hits without extra requests',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=rpc(),cache=new AccountCache(r,{ttlMs:1000});t.after(()=>cache.close());
  const a=key(),b=key(),reads=[];let fetched;
  const original=r.getMultipleAccountsInfoAndContext;
  r.getMultipleAccountsInfoAndContext=async function(keys,c){fetched=keys.map(k=>k.toBase58());return original.call(this,keys,c);};
  cache.record(a.toBase58(),{data:Buffer.from('cached')},100);
  const view=cache.view(100,false,{trace:row=>reads.push(row)});
  await view.getMultipleAccountsInfo([a,b]);
  assert.deepEqual(fetched,[b.toBase58()]);assert.equal(r.calls,1);
  assert.deepEqual(reads[0].accounts.map(a=>a.reason),['hit','absent']);
  assert.equal(reads[0].outcome,'http');assert.equal(reads[0].response_slot,100);
  await view.getMultipleAccountsInfo([a,b]);assert.equal(r.calls,1);
  assert.equal(reads[1].outcome,'cache-hit');
  r.slot=101;await cache.view(101,false,{trace:row=>reads.push(row)}).getMultipleAccountsInfo([a]);
  assert.equal(reads[2].accounts[0].reason,'slot-behind');
  now+=1001;await view.getMultipleAccountsInfo([a]);assert.equal(reads[3].accounts[0].reason,'expired');
  now+=1001;r.getMultipleAccountsInfoAndContext=async()=>{throw Error('private-provider-text');};
  await assert.rejects(view.getMultipleAccountsInfo([a]),/private-provider/);
  assert.equal(reads[4].outcome,'unavailable');assert(!JSON.stringify(reads).includes('private-provider'));
});

test('shared snapshots reuse fresh pre-launch data but still reject expired or disconnected state',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=rpc(),cache=new AccountCache(r,{ttlMs:1000});t.after(()=>cache.close());
  const a=key(),k=a.toBase58(),reads=[];
  cache.record(k,{data:Buffer.from('cached')},90);
  const view=cache.view(100,false,{sharedKeys:[a],trace:row=>reads.push(row)});
  const reason=()=>{view.localCreateShared([a]);return reads.at(-1).accounts[0].reason;};
  assert.equal(reason(),'hit');assert(view.localCreateShared([a])[0]); // no ACK required inside TTL
  now+=1001;assert.equal(reason(),'not-continuous');cache.rows.get(k).continuous=true;
  assert.equal(reason(),'subscription-not-ready');cache.watchState.get(k).subscribed=true;
  assert.equal(reason(),'websocket-unhealthy');r._rpcWebSocketConnected=true;cache.pulseAt=now;
  cache.head=90;assert.equal(reason(),'hit'); // settings need not catch the create slot
  r._rpcWebSocket.emit('close');assert.equal(reason(),'absent');assert.equal(r.calls,0);
});
