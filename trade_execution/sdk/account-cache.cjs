'use strict';
const web3=require('@solana/web3.js');
const ACTIVE=(1n<<64n)-1n;
// On-demand HTTP bootstrap, then public websocket updates. No HTTP refresh loop.
class AccountCache {
  constructor(connection,{ttlMs=2000,maxAccounts=512,commitment='confirmed'}={}){
    this.commitment=commitment;this.rpc=connection;this.ttlMs=ttlMs;this.max=maxAccounts;
    this.rows=new Map();this.misc=new Map();this.subscriptions=new Map();this.watchState=new Map();
    this.sharedWait=new Map();this.pinned=new Set();this.repairs=new Map();this.repairActive=new Set();
    this.losses=new Map();this.repairTimer=null;this.repairScheduled=false;this.repairBusy=false;
    this.readStats={demand_batches:0,demand_accounts:0,warm_batches:0,warm_accounts:0,repair_batches:0,repair_accounts:0,
      null_baselines:0,invalidations:0,evictions:0,subscription_gaps:0};this.readWaiters=new Set();this.wsReadWins=0;
    this.generation=0;this.executionGeneration=0;this.lastInvalidation='none';this.hits=0;this.misses=0;this.closed=false;this.head=0;this.pulseAt=null;this.clock=null;
    connection._rpcWebSocket?.on('close',()=>this.invalidate({reason:'websocket-close'}));
    connection._rpcWebSocket?.on('error',()=>this.invalidate({subscriptionsLost:false,reason:'websocket-error'}));
    if(connection.onSlotChange){
      this.slotId=connection.onSlotChange(({slot})=>{
        if(this.closed||!Number.isSafeInteger(slot)||slot<this.head)return;
        this.head=slot;this.pulseAt=Date.now();
        for(const changed of this.readWaiters)changed();
      });
      this.clockId=connection.onAccountChange(web3.SYSVAR_CLOCK_PUBKEY,(row,context)=>{
        if(this.closed||!row?.data||row.data.length<40)return;
        const slot=Number(row.data.readBigUInt64LE(0)),epoch=Number(row.data.readBigUInt64LE(16));
        if(!Number.isSafeInteger(slot)||!Number.isSafeInteger(epoch)||slot>context.slot)return;
        if(!this.clock||slot>=this.clock.slot)this.clock={slot,epoch,at:Date.now()};
      },commitment);
    }
  }
  healthy(){const age=this.pulseAt===null?-1:Date.now()-this.pulseAt;
    return this.rpc._rpcWebSocketConnected===true&&age>=0&&age<=this.ttlMs;}
  watch(k){
    if(this.closed||this.subscriptions.has(k)||!this.rpc.onAccountChange)return;
    const state={subscribed:false,version:0,dispose:()=>{}};this.watchState.set(k,state);
    const id=this.rpc.onAccountChange(new web3.PublicKey(k),(value,context)=>{
      if(!this.closed&&this.watchState.get(k)===state){
        state.subscribed=true;this.record(k,value,context.slot,this.generation,true);
        for(const changed of this.readWaiters)changed();
      }
    },this.commitment);
    this.subscriptions.set(k,id);
    // Pinned web3.js 1.98.4 exposes this hook (also used by its confirmation code).
    // If absent, snapshots retain their short TTL; never assume an ACK occurred.
    if(this.rpc._onSubscriptionStateChange)state.dispose=this.rpc._onSubscriptionStateChange(id,next=>{
      if(this.watchState.get(k)!==state)return;
      state.version++;state.subscribed=next==='subscribed';
      if(!state.subscribed){this.readStats.subscription_gaps++;this.rows.delete(k);this.noteLoss(k,'subscription-gap');}
      else if(this.sharedWait.has(k))void this.baselineShared(k,state);
    });
    this.trim();
  }
  trim(){
    while(this.rows.size>this.max||this.subscriptions.size>this.max){
      const k=[...this.rows.keys(),...this.subscriptions.keys()].find(k=>!this.pinned.has(k));
      if(k===undefined)break; // Only the fixed protocol settings may be pinned.
      this.readStats.evictions++;this.noteLoss(k,'evicted');this.repairs.delete(k);
      this.sharedWait.delete(k);this.rows.delete(k);const id=this.subscriptions.get(k);this.subscriptions.delete(k);
      this.watchState.get(k)?.dispose();this.watchState.delete(k);
      if(id!==undefined)Promise.resolve(this.rpc.removeAccountChangeListener(id)).catch(()=>{});
    }
  }
  record(k,value,slot,generation=this.generation,continuous=false){
    if(this.closed||generation!==this.generation||!Number.isSafeInteger(slot)||slot<0)return;
    const old=this.rows.get(k);if(old&&slot<old.slot)return;
    this.watch(k);this.rows.delete(k);this.rows.set(k,{value,slot,at:Date.now(),continuous});this.trim();
  }
  valid(row,minSlot,k){
    const age=row?Date.now()-row.at:-1;
    const watched=row?.continuous&&this.watchState.get(k)?.subscribed&&this.healthy();
    // Slot notifications prove connection liveness, NOT newer account contents.
    // A changed pool still needs its own account update at/after the trigger.
    return row&&row.slot>=minSlot&&age>=0&&(age<=this.ttlMs||watched);
  }
  noteLoss(k,reason){
    this.losses.delete(k);this.losses.set(k,{reason,at:Date.now()});
    while(this.losses.size>512)this.losses.delete(this.losses.keys().next().value);
  }
  keepShared(keys,wait=async()=>{},{pin=false}={}){
    for(const key of keys){const k=key.toBase58();if(pin)this.pinned.add(k);this.sharedWait.set(k,wait);this.watch(k);}
    // Check only for missing post-ACK baselines. Healthy rows never cause HTTP.
    if(!this.repairTimer){this.repairTimer=setInterval(()=>this.recoverShared(),1000);this.repairTimer.unref?.();}
  }
  recoverShared(){
    if(this.closed||this.repairBusy)return;
    const batch=[];
    for(const [k,wait] of this.sharedWait){
      const state=this.watchState.get(k),previous=this.repairs.get(k);
      // null is a valid snapshot of an absent account. Its subscription will
      // report creation; it is not a transport failure to poll forever.
      if(state?.subscribed&&!this.rows.get(k)?.continuous&&Date.now()>=(previous?.due||0))
        batch.push({k,state,version:state.version,wait,previous});
      if(batch.length===64)break;
    }
    if(!batch.length)return;
    this.repairBusy=true;for(const {k} of batch)this.repairActive.add(k);
    void require('./catchup-budget.cjs').withCatchupBudget({},()=>this.repairShared(batch));
  }
  baselineShared(){
    // Coalesce ACKs from the same event-loop turn into one account batch.
    if(this.closed||this.repairScheduled)return;
    this.repairScheduled=true;
    queueMicrotask(()=>{this.repairScheduled=false;this.recoverShared();});
  }
  async repairShared(batch){
    const gen=this.generation;
    const current=entry=>!this.closed&&gen===this.generation&&this.watchState.get(entry.k)===entry.state
      &&entry.state.subscribed&&entry.state.version===entry.version;
    try{
      await Promise.all(batch.map(entry=>entry.wait()));
      batch=batch.filter(entry=>current(entry)&&!this.rows.get(entry.k)?.continuous);
      if(!batch.length)return;
      const wait=async()=>{
        await Promise.all(batch.map(entry=>entry.wait()));
        if(!batch.every(current))throw Error('cache-disconnected');
      };
      await this.get(batch.map(({k})=>new web3.PublicKey(k)),{},false,wait,new Set(),true);
      for(const {k} of batch){
        this.repairs.delete(k);
        if(this.rows.get(k)?.value===null)this.readStats.null_baselines++;
      }
    }catch{
      for(const entry of batch)if(current(entry)){
        const failures=Math.min(6,(entry.previous?.failures||0)+1);
        this.repairs.set(entry.k,{failures,due:Date.now()+Math.min(30000,1000*2**(failures-1))});
        this.noteLoss(entry.k,'baseline-failed');
      }
    }finally{this.repairActive.clear();this.repairBusy=false;}
  }
  async primeShared(keys,wait=async()=>{}){
    this.keepShared(keys,wait);
    return this.get(keys,{},false,wait,new Set(),false,null,'warm');
  }
  validShared(row,minSlot,k,shared){
    return this.valid(row,minSlot,k)||(shared.has(k)&&row?.value&&row.continuous
      &&this.watchState.get(k)?.subscribed&&this.healthy()&&this.head>=minSlot&&Date.now()>=row.at);
  }
  evidence(k,minSlot,shared,force=false,local=false){
    const row=this.rows.get(k),age=row?Date.now()-row.at:null,subscribed=!!this.watchState.get(k)?.subscribed;
    const healthy=this.healthy(),isShared=shared.has(k);
    const hit=local?isShared&&row?.value&&this.valid(row,0,k):
      !force&&this.validShared(row,minSlot,k,shared);
    const reason=hit?'hit':force?'forced-baseline':local&&!isShared?'not-shared':!row?'absent':
      age<0?'clock-ahead':local&&!row.value?'null-account':isShared&&!row.continuous?'not-continuous':
      isShared&&!subscribed?'subscription-not-ready':isShared&&!healthy?'websocket-unhealthy':
      isShared&&this.head<(local?minSlot-2:minSlot)?'head-behind':row.slot<minSlot?'slot-behind':'expired';
    return {address:k,source:hit?'cache-hit':'cache-miss',reason,min_slot:minSlot,
      row_slot:row?.slot,age_ms:age,continuous:!!row?.continuous,subscribed,healthy,head:this.head,shared:isShared,ttl_ms:this.ttlMs,
      pinned:this.pinned.has(k),recovery_pending:this.repairActive.has(k),
      recovery_failures:this.repairs.get(k)?.failures||0,
      last_loss:this.losses.get(k)?.reason||'never-loaded',
      websocket_connected:this.rpc._rpcWebSocketConnected===true,pulse_age_ms:this.pulseAt===null?null:Date.now()-this.pulseAt};
  }
  async firstReady(keys,names,config,gen,shared,force,category='demand'){
    const minSlot=config.minContextSlot||0,controller=new AbortController(),executionGeneration=this.executionGeneration;
    let changed,settled=false;
    const websocket=new Promise((resolve,reject)=>{
      changed=()=>{
        if(settled)return;
        if(this.closed||executionGeneration!==this.executionGeneration){settled=true;reject(Error('cache-disconnected'));return;}
        // Losing the account websocket invalidates cached continuity, not the
        // independent HTTP request. Keep its original slot and deadline.
        if(gen!==this.generation)return;
        if(force)return; // Post-ACK baselines must actually complete their HTTP read.
        const rows=names.map(k=>this.rows.get(k));
        if(rows.every((row,i)=>this.validShared(row,minSlot,names[i],shared))){
          settled=true;this.wsReadWins++;
          resolve({context:{slot:Math.min(...rows.map(row=>row.slot))},value:rows.map(row=>row.value),fromWebsocket:true});
        }
      };
      this.readWaiters.add(changed);changed();
    });
    const http=Promise.resolve().then(()=>{
      category=force?'repair':category;
      this.readStats[category+'_batches']++;this.readStats[category+'_accounts']+=keys.length;
      return require('./read-cancel.cjs').withReadSignal(controller.signal,
        ()=>this.rpc.getMultipleAccountsInfoAndContext(keys,{...config,commitment:this.commitment}));
    });
    try{return await Promise.race([websocket,http]);}
    finally{settled=true;this.readWaiters.delete(changed);controller.abort();}
  }
  async get(keys,config={},cacheOnly=false,wait=async()=>{},shared=new Set(),force=false,trace=null,category='demand'){
    const minSlot=config.minContextSlot||0;
    if(!keys.length)return {context:{slot:minSlot},value:[]};
    const names=keys.map(k=>k.toBase58());
    let rows=names.map(k=>this.rows.get(k));
    const missing=names.map((k,i)=>!force&&this.validShared(rows[i],minSlot,k,shared)?-1:i).filter(i=>i>=0);
    const evidence=trace?{phase:'cache',accounts:names.map(k=>this.evidence(k,minSlot,shared,force)),outcome:'cache-hit',ms:0}:null;
    if(evidence)trace(evidence);
    if(!missing.length){this.hits+=keys.length;return {context:{slot:Math.min(...rows.map(r=>r.slot))},value:rows.map(r=>r.value)};}
    if(cacheOnly){if(evidence)evidence.outcome='cache-only-miss';throw Error('price-cache-miss');}
    const beforeWait=this.generation;
    await wait();const gen=this.generation,executionGeneration=this.executionGeneration;
    // A partial cached batch must not reuse pre-wait rows across invalidation.
    if(beforeWait!==gen&&missing.length!==keys.length)throw Error('cache-disconnected');
    this.misses+=missing.length;
    missing.forEach(i=>this.watch(names[i]));
    const versions=missing.map(i=>this.watchState.get(names[i])?.version);
    const acked=missing.map(i=>this.watchState.get(names[i])?.subscribed);
    const began=performance.now();if(evidence)evidence.outcome='pending';
    const result=await this.firstReady(missing.map(i=>keys[i]),missing.map(i=>names[i]),config,gen,shared,force,category).then(result=>{
      if(evidence){evidence.outcome=result.fromWebsocket?'websocket':'http';evidence.response_slot=result.context.slot;}
      return result;
    },error=>{if(evidence)evidence.outcome='unavailable';throw error;})
      .finally(()=>{if(evidence)evidence.ms=Math.round((performance.now()-began)*1000)/1000;});
    if(this.closed||executionGeneration!==this.executionGeneration)throw Error('cache-disconnected');
    if(!Number.isSafeInteger(result.context?.slot)||result.context.slot<minSlot
      ||!Array.isArray(result.value)||result.value.length!==missing.length)throw Error('cache-slot-behind');
    if(gen!==this.generation){
      // Only a complete fresh HTTP batch can finish across a websocket gap.
      // Do not publish it into the new cache generation or mix old cache hits.
      if(result.fromWebsocket||missing.length!==keys.length||force)throw Error('cache-disconnected');
      if(evidence)evidence.continued_after_disconnect=true;
      return result;
    }
    const fetched=new Map(missing.map((index,i)=>[names[index],{value:result.value[i],slot:result.context.slot,at:Date.now(),continuous:false}]));
    if(!result.fromWebsocket)missing.forEach((index,i)=>{
      const state=this.watchState.get(names[index]);
      this.record(names[index],result.value[i],result.context.slot,gen,!!acked[i]&&state?.subscribed&&state.version===versions[i]);
    });
    // Copy the current rows; a websocket update can be newer than the HTTP reply.
    rows=names.map((k,i)=>this.rows.get(k)||fetched.get(k)||rows[i]);
    if(rows.some((row,i)=>!this.validShared(row,minSlot,names[i],shared)))throw Error('cache-slot-behind');
    return {context:{slot:Math.min(...rows.map(r=>r.slot))},value:rows.map(r=>r.value)};
  }
  invalidate({subscriptionsLost=true,reason='generation-invalidated'}={}){
    this.readStats.invalidations++;
    this.lastInvalidation=reason;
    if(!['websocket-close','websocket-error'].includes(reason))this.executionGeneration++;
    for(const k of new Set([...this.rows.keys(),...this.subscriptions.keys()]))this.noteLoss(k,reason);
    this.repairs.clear();
    this.generation++;this.rows.clear();this.misc.clear();this.clock=null;this.pulseAt=null;this.head=0;
    for(const state of this.watchState.values()){
      if(subscriptionsLost)state.subscribed=false;
      state.version++;
    }
    for(const changed of this.readWaiters)changed();
    this.onInvalidate?.();
    // A fork invalidates account contents, not an otherwise active subscription.
    // Re-baseline known shared settings in the background; wait() yields to trades.
    if(!subscriptionsLost)for(const [k,state] of this.watchState)
      if(state.subscribed&&this.sharedWait.has(k))void this.baselineShared(k,state);
  }
  async table(key,config,cacheOnly,wait,trace=null,category='demand'){
    const r=await this.get([key],{...config,minContextSlot:0},cacheOnly,wait,new Set(),false,trace,category),row=r.value[0];
    if(!row||row.executable||!row.owner.equals(web3.AddressLookupTableProgram.programId))throw Error('invalid-tables');
    const state=web3.AddressLookupTableAccount.deserialize(row.data);
    if(state.deactivationSlot!==ACTIVE)throw Error('invalid-tables');
    // Newly appended addresses cannot be used in their extension slot.
    const bankSlot=Math.max(config.minContextSlot||0,r.context.slot);
    const addresses=state.lastExtendedSlot>=bankSlot?state.addresses.slice(0,state.lastExtendedSlotStartIndex):state.addresses;
    return {context:r.context,value:new web3.AddressLookupTableAccount({key,state:{...state,addresses}})};
  }
  view(minSlot=0,cacheOnly=false,{wait=async()=>{},blockhashCache,sharedKeys=[],trace=null,primeReads=false,readCategory='demand'}={}){
    const cache=this,rpc=this.rpc,shared=new Set(sharedKeys.map(k=>k.toBase58()));
    const config=c=>({...typeof c==='object'&&c,minContextSlot:Math.max(minSlot,c?.minContextSlot||0),commitment:cache.commitment});
    const read=(keys,c,sharedKeys=new Set())=>{
      if(primeReads)cache.keepShared(keys,wait);
      return cache.get(keys,c,cacheOnly,wait,sharedKeys,false,trace,readCategory);
    };
    const methods={
      // Verified create only: common configuration and quote-mint metadata
      // can predate the launch. Keep TTL/continuous-WS validity, not a launch-slot
      // barrier. New target state and wallet balances retain strict reads.
      localCreateShared:keys=>{
        if(trace)trace({phase:'shared',accounts:keys.map(key=>cache.evidence(key.toBase58(),minSlot,shared,false,true))});
        return keys.map(key=>{
        const k=key.toBase58(),r=cache.rows.get(k);
        return shared.has(k)&&r?.value&&cache.valid(r,0,k)?r.value:null;
      });},
      quoteSnapshot:()=>cache.view(0,cacheOnly,{wait,blockhashCache,primeReads,readCategory,trace:trace?row=>{row.scope='quote';trace(row);}:null}),
      getMultipleAccountsInfoAndContext:(keys,c)=>read(keys,config(c),shared),
      getMultipleAccountsInfo:async(keys,c)=>(await read(keys,config(c),shared)).value,
      getAccountInfoAndContext:async(k,c)=>{const r=await read([k],config(c));return {context:r.context,value:r.value[0]};},
      getAccountInfo:async(k,c)=>(await read([k],config(c))).value[0],
      getLatestBlockhash:async c=>{
        if(blockhashCache){
          try{return blockhashCache.pick(minSlot);}
          catch(error){if(error.message!=='blockhash-cache-miss')throw error;}
        }
        // A websocket disconnect invalidates the cache, not the HTTP route.
        // A fresh bounded primary/fallback read can still build an exit.
        await wait();return rpc.getLatestBlockhash(config(c));
      },
      simulateTransaction:(tx,c)=>rpc.simulateTransaction(tx,config(c)),
      getAddressLookupTable:(key,c)=>cache.table(key,config(c),cacheOnly,wait,trace,readCategory),
      getEpochInfo:async()=>{
        const r=cache.misc.get('epoch'),age=r?Date.now()-r.at:-1,clock=cache.clock;
        if(r&&((age>=0&&age<=cache.ttlMs&&r.value.absoluteSlot>=minSlot)||
          (cache.healthy()&&clock&&Date.now()-clock.at>=0&&Date.now()-clock.at<=cache.ttlMs&&clock.slot>=minSlot&&clock.epoch===r.value.epoch)))return r.value;
        if(cacheOnly)throw Error('price-cache-miss');
        await wait();const gen=cache.generation,executionGeneration=cache.executionGeneration;
        const value=await rpc.getEpochInfo(config());
        if(cache.closed||executionGeneration!==cache.executionGeneration)throw Error('cache-disconnected');
        if(gen===cache.generation)cache.misc.set('epoch',{at:Date.now(),value});return value;
      },
    };
    return new Proxy(rpc,{get(target,p){
      if(cacheOnly&&!['getMultipleAccountsInfoAndContext','getMultipleAccountsInfo','getAccountInfoAndContext','getAccountInfo','getEpochInfo'].includes(p))
        return typeof target[p]==='function'?()=>{throw Error('price-cache-miss');}:target[p];
      return methods[p]||(typeof target[p]==='function'?async(...args)=>{await wait();return target[p](...args);}:target[p]);
    }});
  }
  close(){
    this.closed=true;clearInterval(this.repairTimer);this.repairTimer=null;
    for(const changed of this.readWaiters)changed();
    this.rows.clear();this.sharedWait.clear();
    for(const state of this.watchState.values())state.dispose();
    for(const id of this.subscriptions.values())Promise.resolve(this.rpc.removeAccountChangeListener(id)).catch(()=>{});
    if(this.clockId!==undefined)Promise.resolve(this.rpc.removeAccountChangeListener(this.clockId)).catch(()=>{});
    if(this.slotId!==undefined)Promise.resolve(this.rpc.removeSlotChangeListener(this.slotId)).catch(()=>{});
    this.subscriptions.clear();this.watchState.clear();
  }
}
module.exports={AccountCache};
