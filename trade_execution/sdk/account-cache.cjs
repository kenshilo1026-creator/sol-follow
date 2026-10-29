'use strict';
const web3=require('@solana/web3.js');
const ACTIVE=(1n<<64n)-1n;
// On-demand HTTP bootstrap, then public websocket updates. No HTTP refresh loop.
class AccountCache {
  constructor(connection,{ttlMs=2000,maxAccounts=512,commitment='confirmed'}={}){
    this.commitment=commitment;this.rpc=connection;this.ttlMs=ttlMs;this.max=maxAccounts;
    this.rows=new Map();this.misc=new Map();this.subscriptions=new Map();this.watchState=new Map();
    this.sharedWait=new Map();this.readWaiters=new Set();this.wsReadWins=0;
    this.generation=0;this.hits=0;this.misses=0;this.closed=false;this.head=0;this.pulseAt=null;this.clock=null;
    connection._rpcWebSocket?.on('close',()=>this.invalidate());
    connection._rpcWebSocket?.on('error',()=>this.invalidate());
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
      if(!state.subscribed)this.rows.delete(k);
      else if(this.sharedWait.has(k))void this.baselineShared(k,state);
    });
    this.trim();
  }
  trim(){
    while(this.rows.size>this.max||this.subscriptions.size>this.max){
      const k=this.rows.keys().next().value||this.subscriptions.keys().next().value;
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
  async baselineShared(k,state){
    // One HTTP baseline after ACK, not periodic polling. An earlier HTTP
    // response cannot prove coverage of the subscription startup gap.
    const version=state.version,gen=this.generation,wait=this.sharedWait.get(k);
    try{
      await wait();
      if(this.closed||gen!==this.generation||!state.subscribed||state.version!==version)return;
      await this.get([new web3.PublicKey(k)],{},false,wait,new Set(),true);
    }catch{} // A failed baseline leaves strict freshness/fallback in force.
  }
  async primeShared(keys,wait=async()=>{}){
    for(const key of keys){const k=key.toBase58();this.sharedWait.set(k,wait);this.watch(k);}
    return this.get(keys,{},false,wait);
  }
  validShared(row,minSlot,k,shared){
    return this.valid(row,minSlot,k)||(shared.has(k)&&row?.value&&row.continuous
      &&this.watchState.get(k)?.subscribed&&this.healthy()&&this.head>=minSlot&&Date.now()>=row.at);
  }
  async firstReady(keys,names,config,gen,shared,force){
    const minSlot=config.minContextSlot||0,controller=new AbortController();
    let changed,settled=false;
    const websocket=new Promise((resolve,reject)=>{
      changed=()=>{
        if(settled)return;
        if(this.closed||gen!==this.generation){settled=true;reject(Error('cache-disconnected'));return;}
        if(force)return; // Post-ACK baselines must actually complete their HTTP read.
        const rows=names.map(k=>this.rows.get(k));
        if(rows.every((row,i)=>this.validShared(row,minSlot,names[i],shared))){
          settled=true;this.wsReadWins++;
          resolve({context:{slot:Math.min(...rows.map(row=>row.slot))},value:rows.map(row=>row.value),fromWebsocket:true});
        }
      };
      this.readWaiters.add(changed);changed();
    });
    const http=Promise.resolve().then(()=>require('./read-cancel.cjs').withReadSignal(controller.signal,
      ()=>this.rpc.getMultipleAccountsInfoAndContext(keys,{...config,commitment:this.commitment})));
    try{return await Promise.race([websocket,http]);}
    finally{settled=true;this.readWaiters.delete(changed);controller.abort();}
  }
  async get(keys,config={},cacheOnly=false,wait=async()=>{},shared=new Set(),force=false){
    const minSlot=config.minContextSlot||0;
    if(!keys.length)return {context:{slot:minSlot},value:[]};
    const names=keys.map(k=>k.toBase58());
    let rows=names.map(k=>this.rows.get(k));
    const missing=names.map((k,i)=>!force&&this.validShared(rows[i],minSlot,k,shared)?-1:i).filter(i=>i>=0);
    if(!missing.length){this.hits+=keys.length;return {context:{slot:Math.min(...rows.map(r=>r.slot))},value:rows.map(r=>r.value)};}
    if(cacheOnly)throw Error('price-cache-miss');
    await wait();const gen=this.generation;this.misses+=missing.length;
    missing.forEach(i=>this.watch(names[i]));
    const versions=missing.map(i=>this.watchState.get(names[i])?.version);
    const acked=missing.map(i=>this.watchState.get(names[i])?.subscribed);
    const result=await this.firstReady(missing.map(i=>keys[i]),missing.map(i=>names[i]),config,gen,shared,force);
    if(gen!==this.generation)throw Error('cache-disconnected');
    if(!result.fromWebsocket&&result.context.slot<minSlot)throw Error('cache-slot-behind');
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
  invalidate(){
    this.generation++;this.rows.clear();this.misc.clear();this.clock=null;this.pulseAt=null;this.head=0;
    for(const state of this.watchState.values()){state.subscribed=false;state.version++;}
    for(const changed of this.readWaiters)changed();
    this.onInvalidate?.();
  }
  async table(key,config,cacheOnly,wait){
    const r=await this.get([key],{...config,minContextSlot:0},cacheOnly,wait),row=r.value[0];
    if(!row||row.executable||!row.owner.equals(web3.AddressLookupTableProgram.programId))throw Error('invalid-tables');
    const state=web3.AddressLookupTableAccount.deserialize(row.data);
    if(state.deactivationSlot!==ACTIVE)throw Error('invalid-tables');
    // Newly appended addresses cannot be used in their extension slot.
    const bankSlot=Math.max(config.minContextSlot||0,r.context.slot);
    const addresses=state.lastExtendedSlot>=bankSlot?state.addresses.slice(0,state.lastExtendedSlotStartIndex):state.addresses;
    return {context:r.context,value:new web3.AddressLookupTableAccount({key,state:{...state,addresses}})};
  }
  view(minSlot=0,cacheOnly=false,{wait=async()=>{},blockhashCache,sharedKeys=[]}={}){
    const cache=this,rpc=this.rpc,shared=new Set(sharedKeys.map(k=>k.toBase58()));
    const config=c=>({...typeof c==='object'&&c,minContextSlot:Math.max(minSlot,c?.minContextSlot||0),commitment:cache.commitment});
    const methods={
      getMultipleAccountsInfoAndContext:(keys,c)=>cache.get(keys,config(c),cacheOnly,wait,shared),
      getMultipleAccountsInfo:async(keys,c)=>(await cache.get(keys,config(c),cacheOnly,wait,shared)).value,
      getAccountInfoAndContext:async(k,c)=>{const r=await cache.get([k],config(c),cacheOnly,wait);return {context:r.context,value:r.value[0]};},
      getAccountInfo:async(k,c)=>(await cache.get([k],config(c),cacheOnly,wait)).value[0],
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
      getAddressLookupTable:(key,c)=>cache.table(key,config(c),cacheOnly,wait),
      getEpochInfo:async()=>{
        const r=cache.misc.get('epoch'),age=r?Date.now()-r.at:-1,clock=cache.clock;
        if(r&&((age>=0&&age<=cache.ttlMs&&r.value.absoluteSlot>=minSlot)||
          (cache.healthy()&&clock&&Date.now()-clock.at>=0&&Date.now()-clock.at<=cache.ttlMs&&clock.slot>=minSlot&&clock.epoch===r.value.epoch)))return r.value;
        if(cacheOnly)throw Error('price-cache-miss');
        await wait();const gen=cache.generation;
        const value=await rpc.getEpochInfo(config());if(gen!==cache.generation)throw Error('cache-disconnected');
        cache.misc.set('epoch',{at:Date.now(),value});return value;
      },
    };
    return new Proxy(rpc,{get(target,p){
      if(cacheOnly&&!['getMultipleAccountsInfoAndContext','getMultipleAccountsInfo','getAccountInfoAndContext','getAccountInfo','getEpochInfo'].includes(p))
        return typeof target[p]==='function'?()=>{throw Error('price-cache-miss');}:target[p];
      return methods[p]||(typeof target[p]==='function'?async(...args)=>{await wait();return target[p](...args);}:target[p]);
    }});
  }
  close(){
    this.closed=true;
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
