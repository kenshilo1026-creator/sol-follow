'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events'),web3=require('@solana/web3.js');
const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
const {BlockhashCache}=require('../../trade_execution/sdk/blockhash-cache.cjs');
const {BackgroundGate}=require('../../trade_execution/sdk/background-gate.cjs');
const fixture=require('../fixtures/local_quote_accounts.json');
const key=()=>web3.Keypair.generate().publicKey;
const tick=()=>new Promise(done=>setImmediate(done));
function socketRpc(){
  let next=0;
  return {_rpcWebSocket:new EventEmitter(),_rpcWebSocketConnected:true,reads:0,slot:100,
    callbacks:new Map(),states:new Map(),removed:[],
    onSlotChange(fn){this.pulse=fn;return 900;},async removeSlotChangeListener(){},
    onAccountChange(k,fn){const id=next++;this.callbacks.set(k.toBase58(),{id,fn});return id;},
    _onSubscriptionStateChange(id,fn){this.states.set(id,fn);return ()=>this.states.delete(id);},
    async removeAccountChangeListener(id){this.removed.push(id);},
    ack(k){this.states.get(this.callbacks.get(k.toBase58()).id)?.('subscribed');},
    update(k,value,slot=this.slot){this.callbacks.get(k.toBase58()).fn(value,{slot});},
    async getMultipleAccountsInfoAndContext(keys,c){this.reads++;this.last=c;return {context:{slot:this.slot},value:keys.map(()=>({data:Buffer.from('state')}))};},
    async getEpochInfo(){return {epoch:7,absoluteSlot:this.slot,slotIndex:2,slotsInEpoch:432000};},
  };
}
test('unchanged subscribed accounts stay cached without HTTP polling; gaps fail closed',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:2000,commitment:'processed'});t.after(()=>cache.close());
  const k=key();cache.watch(k.toBase58());r.ack(k);r.pulse({slot:100});
  await cache.view().getAccountInfo(k);assert.equal(r.reads,1);
  now+=100000;r.pulse({slot:110});
  assert((await cache.view(0,true).getAccountInfo(k)).data);assert.equal(r.reads,1);
  // A slot heartbeat cannot manufacture a newer snapshot for a changed pool.
  await assert.rejects(cache.view(110,true).getAccountInfo(k),/price-cache-miss/);
  r.update(k,{data:Buffer.from('new')},110);
  assert.equal((await cache.view(110,true).getAccountInfo(k)).data.toString(),'new');
  now+=2001;await assert.rejects(cache.view(0,true).getAccountInfo(k),/price-cache-miss/);
  r._rpcWebSocket.emit('close');assert.equal(cache.rows.size,0);
  r.slot=111;r.ack(k);r.pulse({slot:111});await cache.view().getAccountInfo(k);
  assert.equal(r.reads,2);assert.equal(r.last.commitment,'processed');
  assert.equal(cache.timer,undefined);assert.equal(cache.refresh,undefined);
});
test('an HTTP snapshot from before subscription ACK cannot certify continuous coverage',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:1000});t.after(()=>cache.close());
  const k=key();await cache.view().getAccountInfo(k);r.ack(k);
  now+=1001;r.pulse({slot:101});
  await assert.rejects(cache.view(0,true).getAccountInfo(k),/price-cache-miss/);
  r.slot=101;await cache.view().getAccountInfo(k);
  now+=1001;r.pulse({slot:102});await cache.view(0,true).getAccountInfo(k);
  assert.equal(r.reads,2);
});
test('clock websocket keeps an epoch cache valid; epoch rollover needs new data',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  await cache.view().getEpochInfo();
  now+=5000;r.pulse({slot:105});const data=Buffer.alloc(40);data.writeBigUInt64LE(105n,0);data.writeBigUInt64LE(7n,16);
  r.update(web3.SYSVAR_CLOCK_PUBKEY,{data},105);
  assert.equal((await cache.view(105,true).getEpochInfo()).epoch,7);
  data.writeBigUInt64LE(8n,16);r.update(web3.SYSVAR_CLOCK_PUBKEY,{data},105);
  await assert.rejects(cache.view(105,true).getEpochInfo(),/price-cache-miss/);
});
test('ALT bytes are cached and websocket replacement handles extension/deactivation',async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const k=new web3.PublicKey(fixture.recipe.tables[0]);
  const raw=fixture.accounts[k.toBase58()];
  const row={...raw,owner:new web3.PublicKey(raw.owner),data:Buffer.from(raw.data[0],'base64')};
  const parsed=web3.AddressLookupTableAccount.deserialize(row.data);r.slot=Number(parsed.lastExtendedSlot)+10;
  r.getMultipleAccountsInfoAndContext=async()=>{r.reads++;return {context:{slot:r.slot},value:[row]};};
  const first=await cache.view(r.slot).getAddressLookupTable(k);
  const second=await cache.view(r.slot+1).getAddressLookupTable(k);
  assert.equal(r.reads,1);assert.deepEqual(first.value.state.addresses,second.value.state.addresses);
  const extended={...row,data:Buffer.from(row.data)};extended.data.writeBigUInt64LE(BigInt(r.slot+2),12);extended.data[20]=1;
  r.update(k,extended,r.slot+2);
  assert.equal((await cache.view(r.slot+2).getAddressLookupTable(k)).value.state.addresses.length,1);
  assert.equal((await cache.view(r.slot+3).getAddressLookupTable(k)).value.state.addresses.length,parsed.addresses.length);
  const stopped={...row,data:Buffer.from(row.data)};stopped.data.writeBigUInt64LE(BigInt(r.slot+3),4);
  r.update(k,stopped,r.slot+3);await assert.rejects(cache.view().getAddressLookupTable(k),/invalid-tables/);
  assert.equal(r.reads,1);
});
test('background network pauses between awaits; foreground reads proceed',async t=>{
  const gate=new BackgroundGate(),r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const bg=cache.view(0,false,{wait:()=>gate.wait()});await bg.getAccountInfo(key());
  gate.setTrades(1);let done=false;
  const pending=bg.getAccountInfo(key()).then(()=>{done=true;});await tick();
  assert.equal(r.reads,1);assert.equal(done,false);
  await cache.view().getAccountInfo(key());assert.equal(r.reads,2);
  gate.setTrades(0);await pending;assert.equal(r.reads,3);
});
test('shared blockhash is background-only, bounded in age/slot, and invalidated on a fork',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const gate=new BackgroundGate();let calls=0,head=100;
  const r={async getLatestBlockhashAndContext(c){calls++;assert.equal(c.commitment,'processed');return {context:{slot:100},value:{blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:1000}};}};
  const cache=new BlockhashCache(r,{commitment:'processed',refreshMs:999999,isBusy:()=>gate.busy,head:()=>head});t.after(()=>cache.close());
  assert.throws(()=>cache.pick(),/blockhash-cache-miss/);assert.equal(calls,0);
  await Promise.all([cache.refresh(),cache.refresh()]);assert.equal(calls,1);
  const hash=cache.pick(102);assert.deepEqual(cache.pick(103),hash);assert.equal(calls,1);
  gate.setTrades(1);await cache.refresh();assert.equal(calls,1);
  now+=5001;assert.throws(()=>cache.pick(),/blockhash-cache-miss/);assert.equal(calls,1);
  gate.setTrades(0);await cache.refresh();assert.equal(calls,2);
  head=133;assert.throws(()=>cache.pick(),/blockhash-cache-miss/);
  head=100;cache.invalidate();assert.throws(()=>cache.pick(),/blockhash-cache-miss/);
});
test('background blockhash response from a disconnected generation is discarded',async t=>{
  let resolve;const r={getLatestBlockhashAndContext:()=>new Promise(done=>{resolve=done;})};
  const cache=new BlockhashCache(r,{refreshMs:999999});t.after(()=>cache.close());
  const p=cache.refresh();cache.invalidate();resolve({context:{slot:100},value:{blockhash:'hash',lastValidBlockHeight:1000}});await p;
  assert.throws(()=>cache.pick(),/blockhash-cache-miss/);
});


test('only explicitly shared settings reuse older continuously covered snapshots',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:1000});t.after(()=>cache.close());
  const config=key(),pool=key();
  for(const k of [config,pool]){cache.watch(k.toBase58());r.ack(k);}
  r.pulse({slot:100});await cache.view().getMultipleAccountsInfo([config,pool]);
  now+=5000;r.pulse({slot:110});
  const shared=cache.view(110,true,{sharedKeys:[config]});
  assert((await shared.getMultipleAccountsInfo([config]))[0]);assert.equal(r.reads,1);
  await assert.rejects(shared.getMultipleAccountsInfo([pool]),/price-cache-miss/);
  await assert.rejects(cache.view(110,true).getMultipleAccountsInfo([config]),/price-cache-miss/);
  r.update(config,{data:Buffer.from('updated')},110);
  assert.equal((await shared.getMultipleAccountsInfo([config]))[0].data.toString(),'updated');
  now+=1001;await assert.rejects(shared.getMultipleAccountsInfo([config]),/price-cache-miss/);
  r.pulse({slot:111});r._rpcWebSocket.emit('close');
  await assert.rejects(shared.getMultipleAccountsInfo([config]),/price-cache-miss/);
});
test('shared warmup takes one post-ACK baseline and yields that read to trading',async t=>{
  const r=socketRpc(),cache=new AccountCache(r),gate=new BackgroundGate();t.after(()=>cache.close());
  const k=key();await cache.primeShared([k],()=>gate.wait());assert.equal(r.reads,1);
  gate.setTrades(1);r.ack(k);await tick();assert.equal(r.reads,1);
  r.pulse({slot:110});
  await assert.rejects(cache.view(110,true,{sharedKeys:[k]}).getMultipleAccountsInfo([k]),/price-cache-miss/);
  gate.setTrades(0);await tick();assert.equal(r.reads,2);
  await cache.view(110,true,{sharedKeys:[k]}).getMultipleAccountsInfo([k]);
  await tick();assert.equal(r.reads,2); // no periodic HTTP refresh
});


function stalledRpc(r){
  const pending=[];
  r.getMultipleAccountsInfoAndContext=(keys,config)=>{
    const signal=require('../../trade_execution/sdk/read-cancel.cjs').readSignal();
    return new Promise((resolve,reject)=>{
      pending.push({keys,config,signal,resolve,reject});
      signal.addEventListener('abort',()=>reject(Error('cancelled-read')),{once:true});
    });
  };
  return pending;
}
test('fresh complete WS batch replaces lagging HTTP and cancels only that read',async t=>{
  const r=socketRpc(),cache=new AccountCache(r,{commitment:'processed'});t.after(()=>cache.close());
  const pending=stalledRpc(r),a=key(),b=key(),other=key();
  let done=false;
  const read=cache.view(101).getMultipleAccountsInfo([a,b]).then(rows=>{done=true;return rows;});
  const parallel=cache.view(101).getAccountInfo(other);
  await tick();assert.equal(pending.length,2);
  assert.equal(pending[0].config.commitment,'processed');
  r.update(a,{data:Buffer.from('a')},101);r.update(b,{data:Buffer.from('old')},100);
  r.pulse({slot:105});await tick();assert.equal(done,false); // heartbeat/partial batch is not state proof
  r.update(b,{data:Buffer.from('b')},101);
  assert.deepEqual((await read).map(row=>row.data.toString()),['a','b']);
  assert.equal(pending[0].signal.aborted,true);assert.equal(pending[1].signal.aborted,false);
  pending[1].resolve({context:{slot:101},value:[{data:Buffer.from('other')}]});
  assert.equal((await parallel).data.toString(),'other');
  assert.equal(cache.wsReadWins,1);assert.equal(cache.readWaiters.size,0);
});
test('WS race rejects disconnect and cleans up pending HTTP and listeners',async t=>{
  for(const mode of ['invalidate','close']){
    const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
    const pending=stalledRpc(r),p=cache.view(101).getAccountInfo(key());
    const rejected=assert.rejects(p,/cache-disconnected/);
    await tick();cache[mode]();await rejected;
    assert(pending[0].signal.aborted);assert.equal(cache.readWaiters.size,0);
  }
});
test('failed HTTP with incomplete WS evidence fails closed; synchronous errors do not leak waiters',async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const pending=stalledRpc(r),a=key(),b=key();
  const read=cache.view(101).getMultipleAccountsInfo([a,b]);
  const rejected=assert.rejects(read,/offline/);await tick();
  r.update(a,{data:Buffer.from('a')},101);pending[0].reject(Error('offline'));await rejected;
  assert.equal(cache.readWaiters.size,0);assert.equal(cache.wsReadWins,0);
  r.getMultipleAccountsInfoAndContext=()=>{throw Error('sync-failure');};
  await assert.rejects(cache.view(101).getAccountInfo(b),/sync-failure/);
  assert.equal(cache.readWaiters.size,0);
});
test('forced post-ACK baseline cannot be bypassed by the WS race',async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const pending=stalledRpc(r),a=key();let done=false;
  const read=cache.get([a],{minContextSlot:101},false,async()=>{},new Set(),true).then(x=>{done=true;return x;});
  await tick();r.update(a,{data:Buffer.from('ws')},101);await tick();assert.equal(done,false);
  pending[0].resolve({context:{slot:101},value:[{data:Buffer.from('baseline')}]});
  await read;assert.equal(cache.wsReadWins,0);assert.equal(cache.readWaiters.size,0);
});


test('fork clears old data but restores shared snapshots without a second subscription ACK',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:1000}),gate=new BackgroundGate();
  t.after(()=>cache.close());
  const k=key();await cache.primeShared([k],()=>gate.wait());r.ack(k);await tick();
  r.pulse({slot:100});assert.equal(cache.rows.get(String(k)).continuous,true);
  const before=r.reads;gate.enter();cache.invalidate({subscriptionsLost:false});
  assert.equal(cache.rows.size,0);assert.equal(cache.watchState.get(String(k)).subscribed,true);
  await tick();assert.equal(r.reads,before); // No read steals foreground capacity.
  r.slot=102;r.pulse({slot:102});gate.leave();await tick();
  assert.equal(r.reads,before+1);assert.equal(cache.rows.get(String(k)).continuous,true);
  now+=10000;r.pulse({slot:103});
  const view=cache.view(103,true,{sharedKeys:[k]});
  assert.equal((await view.getMultipleAccountsInfo([k]))[0].data.toString(),'state');
  assert.equal(r.reads,before+1); // Subsequent create reuses the new baseline, no polling.
  cache.invalidate();
  assert.equal(cache.watchState.get(String(k)).subscribed,false);
  await tick();assert.equal(r.reads,before+1); // Real disconnect must await new ACK.
  await assert.rejects(view.getMultipleAccountsInfo([k]),/price-cache-miss/);
});

test('fork baseline does not repeat a foreground read or accept a cancelled old baseline',async t=>{
  const r=socketRpc(),cache=new AccountCache(r),gate=new BackgroundGate();t.after(()=>cache.close());
  const k=key();await cache.primeShared([k],()=>gate.wait());r.ack(k);await tick();
  gate.enter();cache.invalidate({subscriptionsLost:false});
  r.slot=103;await cache.view(103).getAccountInfo(k);
  const reads=r.reads;gate.leave();await tick();assert.equal(r.reads,reads);
  let resolve;r.getMultipleAccountsInfoAndContext=()=>new Promise(done=>resolve=done);
  cache.invalidate({subscriptionsLost:false});await tick();cache.invalidate();
  resolve({context:{slot:104},value:[{data:Buffer.from('old-generation')}]});await tick();
  assert.equal(cache.rows.size,0);
});


test('quote snapshot uses fresh pre-launch pools while target and wallet reads retain the source slot',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:1000});t.after(()=>cache.close());
  const pool=key();await cache.view(0).getAccountInfo(pool);assert.equal(r.reads,1);
  const diagnostics=[];
  const live=cache.view(200,false,{trace:row=>diagnostics.push(row)}),quote=live.quoteSnapshot();
  await quote.getAccountInfo(pool);assert.equal(r.reads,1);
  assert.equal(diagnostics.at(-1).scope,'quote');assert.equal(diagnostics.at(-1).outcome,'cache-hit');
  r.slot=200;await live.getAccountInfo(pool);assert.equal(r.reads,2);assert.equal(r.last.minContextSlot,200);
  now+=1001;await quote.getAccountInfo(pool);assert.equal(r.reads,3);assert.equal(r.last.minContextSlot,0);
  assert.equal(diagnostics.at(-1).scope,'quote');assert.equal(diagnostics.at(-1).outcome,'http');
  r._rpcWebSocket.emit('close');await quote.getAccountInfo(pool);assert.equal(r.reads,4);
});


test('quote warming baselines after ACK in the background and reuses unchanged pools beyond TTL',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r,{ttlMs:1000}),gate=new BackgroundGate();t.after(()=>cache.close());
  const k=key(),warm=cache.view(0,false,{primeReads:true,wait:()=>gate.wait()}).quoteSnapshot();
  await warm.getAccountInfo(k);assert.equal(r.reads,1);
  gate.enter();r.ack(k);await tick();assert.equal(r.reads,1);
  gate.leave();await tick();assert.equal(r.reads,2);assert(cache.rows.get(String(k)).continuous);
  now+=5000;r.pulse({slot:110});
  await cache.view(200).quoteSnapshot().getAccountInfo(k);assert.equal(r.reads,2);
  r._rpcWebSocket.emit('close');assert.equal(cache.rows.size,0);
});


test('core shared settings survive route churn while ordinary cache remains bounded',async t=>{
  const r=socketRpc(),cache=new AccountCache(r,{maxAccounts:3});t.after(()=>cache.close());
  const core=[key(),key()];cache.keepShared(core,async()=>{},{pin:true});
  await cache.primeShared(core);for(const k of core)r.ack(k);await tick();
  for(let i=0;i<10;i++)await cache.view().getAccountInfo(key());
  for(const k of core){assert(cache.rows.has(String(k)));assert(cache.subscriptions.has(String(k)));}
  assert.equal(cache.rows.size,3);assert.equal(cache.subscriptions.size,3);
});

test('failed baseline retries with backoff, yields to trades, and stops reading after recovery',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r),gate=new BackgroundGate();t.after(()=>cache.close());
  const k=key();let fail=true;
  const original=r.getMultipleAccountsInfoAndContext;
  r.getMultipleAccountsInfoAndContext=async function(...args){if(fail){this.reads++;throw Error('offline');}return original.apply(this,args);};
  cache.keepShared([k],()=>gate.wait(),{pin:true});r.ack(k);await tick();
  assert.equal(r.reads,1);assert.equal(cache.repairs.get(String(k)).failures,1);
  cache.recoverShared();await tick();assert.equal(r.reads,1);
  now+=1000;gate.enter();cache.recoverShared();await tick();assert.equal(r.reads,1);
  // Background recovery must never inherit a buy's expired retry budget.
  fail=false;gate.leave();await tick();assert.equal(r.reads,2);assert(cache.rows.get(String(k)).continuous);
  for(let i=0;i<5;i++){now+=30000;r.pulse({slot:100+i});cache.recoverShared();await tick();}
  assert.equal(r.reads,2);assert.equal(cache.repairs.size,0);
  r._rpcWebSocket.emit('close');cache.recoverShared();await tick();assert.equal(r.reads,2);
  r.ack(k);await tick();assert.equal(r.reads,3);assert(cache.rows.get(String(k)).continuous);
});

test('recovery is deduplicated and bounded and null shared accounts retry',async t=>{
  let now=10000;t.mock.method(Date,'now',()=>now);
  const r=socketRpc(),cache=new AccountCache(r),gate=new BackgroundGate();t.after(()=>cache.close());
  const keys=Array.from({length:8},key);gate.enter();cache.keepShared(keys,()=>gate.wait());
  for(const k of keys)r.ack(k);
  assert.equal(cache.repairActive.size,4);cache.recoverShared();assert.equal(cache.repairActive.size,4);
  gate.leave();await tick();cache.recoverShared();await tick();assert.equal(r.reads,8);
  const k=keys[0];cache.rows.delete(String(k));
  r.getMultipleAccountsInfoAndContext=async()=>{r.reads++;return {context:{slot:100},value:[null]};};
  cache.recoverShared();await tick();assert.equal(cache.repairs.get(String(k)).failures,1);
  cache.recoverShared();await tick();assert.equal(r.reads,9);
  now+=1000;cache.recoverShared();await tick();assert.equal(r.reads,10);
  cache.close();now+=30000;cache.recoverShared();await tick();assert.equal(r.reads,10);
});

test('recovery starts a background budget even when registered from a buy',async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const budgets=require('../../trade_execution/sdk/catchup-budget.cjs'),k=key();
  const original=r.getMultipleAccountsInfoAndContext;
  r.getMultipleAccountsInfoAndContext=async function(...args){assert.equal(budgets.isBuy(),false);return original.apply(this,args);};
  budgets.withCatchupBudget({buySignalExpiresAtMs:Date.now()+100},()=>{cache.keepShared([k]);r.ack(k);});
  await tick();assert.equal(r.reads,1);
});


test('socket error without close re-baselines acknowledged settings without awaiting another ACK',async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());
  const k=key();cache.keepShared([k]);r.ack(k);await tick();assert.equal(r.reads,1);
  r._rpcWebSocket.emit('error',Error('temporary'));await tick();
  assert.equal(r.reads,2);assert(cache.rows.get(String(k)).continuous);
  const evidence=cache.evidence(String(k),0,new Set([String(k)]));
  assert.equal(evidence.last_loss,'websocket-error');
});


test('missing baseline actually recovers on background timer without another request', {timeout:4000},async t=>{
  const r=socketRpc(),cache=new AccountCache(r);t.after(()=>cache.close());const k=key();
  const original=r.getMultipleAccountsInfoAndContext;let recovered;
  const done=new Promise(resolve=>recovered=resolve);
  r.getMultipleAccountsInfoAndContext=async function(...args){
    if(this.reads===0){this.reads++;throw Error('first-read-failed');}
    const result=await original.apply(this,args);recovered();return result;
  };
  cache.keepShared([k]);r.ack(k);await tick();assert.equal(r.reads,1);
  // Make the scheduled repair eligible immediately; only the timer runs it.
  cache.repairs.get(String(k)).due=0;
  cache.repairTimer.ref(); // Keep this test alive; production timers intentionally unref.
  await done;await tick();assert.equal(r.reads,2);assert(cache.rows.get(String(k)).continuous);
  cache.close();assert.equal(cache.repairTimer,null);
});
