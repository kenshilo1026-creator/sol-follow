'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js');
const {scheduleSubscriptions}=require('../../trade_execution/sdk/subscription-scheduler.cjs');
const {watchWebsocket}=require('../../trade_execution/sdk/public-rpc.cjs');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function fixture(t,call,options={}){
  const c=new web3.Connection('http://127.0.0.1:1');
  c._rpcWebSocketConnected=true;
  c._rpcWebSocket.call=call;
  c._rpcWebSocket.close=()=>{};
  const events=[],g=scheduleSubscriptions(c,{retryMs:30,maxRetryMs:120,event:r=>events.push(r),...options});
  watchWebsocket(c._rpcWebSocket,()=>{},g.failed);
  t.mock.method(console,'error',()=>{});
  t.after(()=>{g.close();clearTimeout(c._rpcWebSocketIdleTimeout);});
  return {c,g,events};
}
const key=n=>new web3.PublicKey(Buffer.alloc(32,n));
test('pinned web3 immediate subscription rejection yields to timers and retries with a bound',async t=>{
  let calls=0,ok=false;
  const {c,events}=fixture(t,async()=>{calls++;if(!ok)throw {code:-32005,message:'secret-url'};return 44;});
  const id=c.onAccountChange(key(1),()=>{}),states=[];
  c._onSubscriptionStateChange(id,s=>states.push(s));
  await sleep(20);assert.equal(calls,1); // This timer never ran before the fix.
  assert(states.includes('pending'));assert(!states.includes('subscribed'));
  ok=true;await sleep(80);
  assert.equal(calls,2);assert(states.includes('subscribed'));
  assert.equal(events.length,1);assert(events[0].retry_ms>=0&&events[0].retry_ms<=30);
  assert.deepEqual({...events[0],retry_ms:30},{event:'subscription-retry',method:'accountSubscribe',retry_ms:30,rpc_code:-32005});
});
test('concurrent subscribe failures share cooldown and coalesced passes',async t=>{
  let calls=0;
  const {c}=fixture(t,async()=>{calls++;throw {code:429};},{retryMs:100,maxRetryMs:200});
  for(let i=1;i<=20;i++)c.onAccountChange(key(i),()=>{});
  await sleep(30);assert.equal(calls,20);
  await sleep(100);assert.equal(calls,40);
});
test('a pending ACK does not hold newer subscriptions or their ACKs',async t=>{
  let release,calls=0;
  const {c}=fixture(t,async()=>{calls++;if(calls===1)return new Promise(r=>release=r);return 12;});
  c.onAccountChange(key(1),()=>{});await sleep(10);
  const id=c.onAccountChange(key(2),()=>{}),states=[];c._onSubscriptionStateChange(id,s=>states.push(s));
  await sleep(20);assert.equal(calls,2);assert(states.includes('subscribed'));
  release(11);await sleep(10);
});
test('unsubscribe rejection is bounded and recovers without losing subscription state',async t=>{
  let calls=0,ok=false;
  const {c}=fixture(t,async method=>{
    if(method==='accountSubscribe')return 42;
    calls++;if(!ok)throw Error('private-error');return true;
  });
  const id=c.onAccountChange(key(3),()=>{});await sleep(15);
  await c.removeAccountChangeListener(id);await sleep(15);assert.equal(calls,1);
  ok=true;await sleep(80);assert.equal(calls,2);
  assert.equal(Object.keys(c._subscriptionsByHash).length,0);
});
