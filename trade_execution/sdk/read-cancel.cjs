'use strict';
// Scope cancellation to one cache read, never another concurrent build or WS.
const {AsyncLocalStorage}=require('node:async_hooks');
const reads=new AsyncLocalStorage();
const withReadSignal=(signal,work)=>reads.run(signal,work);
const readSignal=()=>reads.getStore();
module.exports={withReadSignal,readSignal};
