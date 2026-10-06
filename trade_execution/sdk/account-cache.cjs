'use strict';
const web3=require('@solana/web3.js');
// Successful public-RPC snapshots only. Every request retains its minContextSlot
// barrier. Websocket updates invalidate bootstrap snapshots across disconnects.
class AccountCache {
  constructor(connection,{ttlMs=2000,maxAccounts=512,refreshMs=1000}={}){
    this.rpc=connection;this.ttlMs=ttlMs;this.max=maxAccounts;this.rows=new Map();this.misc=new Map();
    this.generation=0;this.hits=0;this.misses=0;this.refreshing=false;this.closed=false;this.subscriptions=new Map();
    connection._rpcWebSocket?.on('close',()=>{this.generation++;this.rows.clear();this.misc.clear();});
    this.timer=setInterval(()=>this.refresh().catch(()=>{}),refreshMs);this.timer.unref();
  }
  record(k,value,slot,generation=this.generation){
    if(this.closed||generation!==this.generation||!Number.isSafeInteger(slot)||slot<0)return;
    const old=this.rows.get(k);
    if(old&&slot<old.slot)return;
    this.rows.delete(k);this.rows.set(k,{value,slot,at:Date.now()});
    if(!this.subscriptions.has(k)&&this.rpc.onAccountChange){
      const id=this.rpc.onAccountChange(new web3.PublicKey(k),(row,context)=>{
        if(!this.closed&&this.subscriptions.has(k))this.record(k,row,context.slot);
      },'confirmed');
      this.subscriptions.set(k,id);
    }
    while(this.rows.size>this.max||this.subscriptions.size>this.max){
      const oldKey=this.subscriptions.keys().next().value||this.rows.keys().next().value;this.rows.delete(oldKey);
      const id=this.subscriptions.get(oldKey);this.subscriptions.delete(oldKey);
      if(id!==undefined)Promise.resolve(this.rpc.removeAccountChangeListener(id)).catch(()=>{});
    }
  }
  valid(row,minSlot){const age=row?Date.now()-row.at:-1;return row&&row.slot>=minSlot&&age>=0&&age<=this.ttlMs;}
  async get(keys,config={}){
    const minSlot=config.minContextSlot||0;
    if(!keys.length)return {context:{slot:minSlot},value:[]};
    const rows=keys.map(k=>this.rows.get(k.toBase58()));
    if(rows.every(r=>this.valid(r,minSlot))){this.hits+=keys.length;return {context:{slot:Math.min(...rows.map(r=>r.slot))},value:rows.map(r=>r.value)};}
    this.misses+=keys.length;const gen=this.generation;
    const result=await this.rpc.getMultipleAccountsInfoAndContext(keys,{...config,commitment:'confirmed'});
    if(gen!==this.generation)throw Error('cache-disconnected');
    if(result.context.slot<minSlot)throw Error('cache-slot-behind');
    keys.forEach((k,i)=>this.record(k.toBase58(),result.value[i],result.context.slot,gen));
    return result;
  }
  async refresh(){
    if(this.closed||this.refreshing||!this.rows.size)return;
    this.refreshing=true;
    try{
      const gen=this.generation;
      const keys=[...this.rows.entries()].sort((a,b)=>a[1].at-b[1].at).slice(0,100).map(([k])=>new web3.PublicKey(k));
      const r=await this.rpc.getMultipleAccountsInfoAndContext(keys,{commitment:'confirmed'});
      keys.forEach((k,i)=>this.record(k.toBase58(),r.value[i],r.context.slot,gen));
    }finally{this.refreshing=false;}
  }
  view(minSlot=0){
    const cache=this,rpc=this.rpc;
    const config=c=>({...typeof c==='object'&&c,minContextSlot:Math.max(minSlot,c?.minContextSlot||0),commitment:'confirmed'});
    const methods={
      getMultipleAccountsInfoAndContext:(keys,c)=>cache.get(keys,config(c)),
      getMultipleAccountsInfo:async(keys,c)=>(await cache.get(keys,config(c))).value,
      getAccountInfoAndContext:async(k,c)=>{const r=await cache.get([k],config(c));return {context:r.context,value:r.value[0]};},
      getAccountInfo:async(k,c)=>(await cache.get([k],config(c))).value[0],
      getLatestBlockhash:c=>rpc.getLatestBlockhash(config(c)),
      simulateTransaction:(tx,c)=>rpc.simulateTransaction(tx,config(c)),
      getAddressLookupTable:(key,c)=>rpc.getAddressLookupTable(key,config(c)),
      getEpochInfo:async()=>{
        const r=cache.misc.get('epoch');
        if(r&&Date.now()-r.at<1000&&r.value.absoluteSlot>=minSlot)return r.value;
        const value=await rpc.getEpochInfo(config());cache.misc.set('epoch',{at:Date.now(),value});return value;
      },
    };
    // Other operations (simulation, final blockhash, ALT proof) retain SDK RPC
    // semantics; only account and epoch reads use this cache.
    return new Proxy(rpc,{get(target,p){return methods[p]|| (typeof target[p]==='function'?target[p].bind(target):target[p]);}});
  }
  close(){this.closed=true;clearInterval(this.timer);this.rows.clear();
    for(const id of this.subscriptions.values())Promise.resolve(this.rpc.removeAccountChangeListener(id)).catch(()=>{});
    this.subscriptions.clear();}
}
module.exports={AccountCache};
