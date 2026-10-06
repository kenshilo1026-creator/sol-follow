'use strict';
// One public background read shared by all builds. No cache-miss HTTP in pick().
class BlockhashCache {
  constructor(rpc,{commitment='confirmed',ttlMs=5000,refreshMs=1000,isBusy=()=>false,head=()=>0}={}){
    this.rpc=rpc;this.commitment=commitment;this.ttlMs=ttlMs;this.isBusy=isBusy;this.head=head;
    this.row=null;this.pending=null;this.generation=0;this.closed=false;this.hits=0;this.misses=0;
    this.timer=setInterval(()=>{void this.refresh().catch(()=>{});},refreshMs);this.timer.unref();
  }
  invalidate(){this.generation++;this.row=null;}
  async refresh(){
    if(this.closed||this.isBusy())return;
    if(this.pending)return this.pending;
    const generation=this.generation;
    this.pending=(async()=>{
      const r=await this.rpc.getLatestBlockhashAndContext({commitment:this.commitment});
      if(this.closed||generation!==this.generation)return;
      if(!Number.isSafeInteger(r.context?.slot)||r.context.slot<0||typeof r.value?.blockhash!=='string'
        ||!Number.isSafeInteger(r.value.lastValidBlockHeight)||r.value.lastValidBlockHeight<=0)throw Error('blockhash-cache-miss');
      if(this.row&&r.context.slot<this.row.slot)return;
      this.row={slot:r.context.slot,value:r.value,at:Date.now()};
    })();
    try{return await this.pending;}finally{this.pending=null;}
  }
  pick(minSlot=0){
    const r=this.row,age=r?Date.now()-r.at:-1;
    // A recent hash can precede the trigger slot. Do not confuse this with the
    // minContextSlot barrier on pool reads / simulation / submission.
    if(!r||age<0||age>this.ttlMs||Math.max(minSlot,this.head())-r.slot>32){
      this.misses++;throw Error('blockhash-cache-miss');
    }
    this.hits++;return {...r.value};
  }
  close(){this.closed=true;clearInterval(this.timer);this.invalidate();}
}
module.exports={BlockhashCache};
