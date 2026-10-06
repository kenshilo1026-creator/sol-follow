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
  await assert.rejects(cache.view(200).getAccountInfo(a),/cache-disconnected/);assert.equal(cache.rows.size,0);
});
test('real JSON-lines worker shares state across requests and enforces a newer slot', {timeout:15000},async t=>{
  const http=require('node:http'),{spawn}=require('node:child_process'),readline=require('node:readline'),path=require('node:path');
  let calls=0;
  const server=http.createServer((req,res)=>{
    let body='';req.on('data',c=>body+=c);req.on('end',()=>{
      const request=JSON.parse(body);calls++;
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
  const pending=new Map();lines.on('line',line=>{const r=JSON.parse(line);pending.get(r.id)?.(r);});
  const input={route:'prime',rpc:`http://127.0.0.1:${server.address().port}`,ws:`ws://127.0.0.1:${server.address().port}`,minSlot:100,lookupTables:[],
    primeAccounts:[key().toBase58()],cache:{ttlMs:2000,refreshMs:999999,maxAccounts:32}};
  async function request(id,minSlot){const response=new Promise(r=>pending.set(id,r));child.stdin.write(JSON.stringify({id,input:{...input,minSlot}})+'\n');return response;}
  assert.equal((await request(1,100)).result.warmed,true);assert.equal(calls,1);
  assert.equal((await request(2,100)).stats.hits,1);assert.equal(calls,1);
  assert.equal((await request(3,101)).result.warmed,true);assert.equal(calls,2);
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
