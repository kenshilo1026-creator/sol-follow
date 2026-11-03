'use strict';
// web3.js 1.98.4 recursively awaits _updateSubscriptions on every success and
// failure. Coalesce those requests into bounded timer turns, never a promise
// chain that can monopolize stdin or grow indefinitely on immediate rejection.
function scheduleSubscriptions(connection,{retryMs=1000,maxRetryMs=30000,event=()=>{}}={}){
  const update=connection._updateSubscriptions.bind(connection);
  let running=false,queued=false,timer=null,closed=false,retryAt=0,failures=0;
  function arm(){
    if(closed||running||timer||!queued)return;
    timer=setTimeout(run,Math.max(0,retryAt-Date.now()));timer.unref?.();
  }
  function run(){
    timer=null;if(closed)return;
    running=true;queued=false;
    // Start the pass but do not serialize new subscriptions behind network ACKs.
    // web3 marks each subscription 'subscribing' before its first await.
    let work;
    try{work=update();}
    catch{failed('unknown',null);queued=true;}
    finally{running=false;arm();}
    Promise.resolve(work).catch(()=>{
      if(closed)return;
      failed('unknown',null);queued=true;arm();
    });
  }
  function failed(method,error){
    // Concurrent failures in one pass share one cooldown, not N escalations.
    if(Date.now()>=retryAt){
      if(Date.now()-retryAt>maxRetryMs)failures=0;
      failures=Math.min(6,failures+1);
      retryAt=Date.now()+Math.min(maxRetryMs,retryMs*2**(failures-1));
    }
    const safeMethod=typeof method==='string'&&/^[A-Za-z]{1,48}$/.test(method)?method:'unknown';
    const code=error?.code;
    event({event:'subscription-retry',method:safeMethod,retry_ms:Math.max(0,retryAt-Date.now()),
      ...(Number.isSafeInteger(code)?{rpc_code:code}:{})});
  }
  connection._updateSubscriptions=async()=>{queued=true;arm();};
  return {failed,close(){closed=true;clearTimeout(timer);timer=null;}};
}
module.exports={scheduleSubscriptions};
