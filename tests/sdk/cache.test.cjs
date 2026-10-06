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
test('background refresh is bounded and old response cannot regress an account',async t=>{
  const r=rpc(),cache=new AccountCache(r,{maxAccounts:200,refreshMs:999999});t.after(()=>cache.close());
  for(let i=0;i<110;i++)cache.record(key().toBase58(),{},100);
  let batch;r.getMultipleAccountsInfoAndContext=async(keys)=>{batch=keys.length;return {context:{slot:90},value:keys.map(()=>({old:true}))};};
  await cache.refresh();assert.equal(batch,100);assert([...cache.rows.values()].every(r=>r.slot===100&&!r.value.old));
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
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const child=spawn(process.execPath,[path.resolve(__dirname,'../../trade_execution/sdk/worker.cjs')],{stdio:['pipe','pipe','ignore']});
  const exited=new Promise(r=>child.once('exit',r));
  t.after(async()=>{child.kill();await exited;server.closeAllConnections();await new Promise(r=>server.close(r));});
  const lines=readline.createInterface({input:child.stdout});
  const pending=new Map();lines.on('line',line=>{const r=JSON.parse(line);pending.get(r.id)?.(r);});
  const input={route:'prime',rpc:`http://127.0.0.1:${server.address().port}`,minSlot:100,lookupTables:[],
    primeAccounts:[key().toBase58()],cache:{ttlMs:2000,refreshMs:999999,maxAccounts:32}};
  async function request(id,minSlot){const response=new Promise(r=>pending.set(id,r));child.stdin.write(JSON.stringify({id,input:{...input,minSlot}})+'\n');return response;}
  assert.equal((await request(1,100)).result.warmed,true);assert.equal(calls,1);
  assert.equal((await request(2,100)).stats.hits,1);assert.equal(calls,1);
  assert.equal((await request(3,101)).result.warmed,true);assert.equal(calls,2);
});
