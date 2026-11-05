'use strict';
const bootStarted=performance.now();
// Persistent public state and unsigned builds. No key loading or broadcast.
const readline=require('node:readline'),web3=require('@solana/web3.js');
const {build,connectionFor,REASONS}=require('./build.cjs');
const {AccountCache}=require('./account-cache.cjs');
const {BlockhashCache}=require('./blockhash-cache.cjs');
const {BackgroundGate}=require('./background-gate.cjs');
const {quoteMaximumUsd,quoteMinimumUsd}=require('./observed-buy.cjs');
const {marketCap,ORACLES}=require('./market-cap.cjs');
const {withReporter}=require('./public-rpc.cjs');
const createFast=require('./create-fast.cjs');
const gate=new BackgroundGate();
let cache,blocks,rpcUrl,cacheGeneration;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
const cacheStats=()=>({hits:cache.hits,misses:cache.misses,accounts:cache.rows.size,wsReadWins:cache.wsReadWins,
  accountReads:cache.readStats,blockhashHits:blocks.hits,blockhashMisses:blocks.misses});
// Telemetry only: no RPC and no need for a build request to observe recovery load.
const statsTimer=setInterval(()=>{if(cache)send({event:'cache-stats',stats:cacheStats()});},60000);
statsTimer.unref();
async function handle(message){
  const {id,input}=message;
  const background=input.prewarm||['warm_limit','bootstrap','shared_warm','quote_warm'].includes(input.operation);
  const foreground=input.operation!=='priority'&&!background;
  const received=performance.now();
  require('./build-diagnostics.cjs').init(input);
  const stages=new Set(['getMultipleAccountsInfo','getMultipleAccountsInfoAndContext','getAccountInfo',
    'getAccountInfoAndContext','getLatestBlockhash','getAddressLookupTable','getEpochInfo','simulateTransaction','localCreateShared']);
  const progress=(stage,phase='start')=>{
    if(input.operation==='build'&&!background)
      send({id,progress:{stage,phase,ms:Math.round((performance.now()-received)*1000)/1000}});
  };
  let generation,executionGeneration;
  const cacheLifecycle=()=>{if(cache&&generation!==undefined)input._buildEvidence.cache_lifecycle={
    generation_start:generation,generation_end:cache.generation,
    execution_generation_start:executionGeneration,execution_generation_end:cache.executionGeneration,
    last_invalidation:cache.lastInvalidation};};
  if(foreground)gate.enter();
  try{
    progress('received');
    if(input.activeTrades!==undefined)gate.setTrades(input.activeTrades);
    if(input.operation==='priority'){gate.setTrades(input.active);send({id,result:{priority:input.active}});return;}
    if(!cache){
      rpcUrl=input.rpc;
      const rpc=connectionFor({...input,minSlot:0});
      cache=new AccountCache(rpc,{...input.cache,commitment:input.commitment||'confirmed'});
      blocks=new BlockhashCache(rpc,{...input.blockhashCache,commitment:input.commitment||'confirmed',
        isBusy:()=>gate.busy,head:()=>cache.head});
      cache.onInvalidate=event=>blocks.invalidate(event);
    }
    if(input.rpc!==rpcUrl)throw Error('sdk-or-rpc-failed');
    if(!Number.isSafeInteger(input.minSlot)||input.minSlot<0)throw Error('invalid-slot');
    if(cacheGeneration!==input.cacheGeneration){cache.invalidate({subscriptionsLost:false});cacheGeneration=input.cacheGeneration;}
    // Register core settings even if blockhash bootstrap fails; ordinary route
    // churn must not evict them. Recovery always yields to active executions.
    cache.keepShared(createFast.common,()=>gate.wait(),{pin:true});
    cache.keepSettings(createFast.settings(input),()=>gate.wait());
    generation=cache.generation;executionGeneration=cache.executionGeneration;
    const wait=background?()=>gate.wait():async()=>{};
    const cacheOnly=['quote_limit','quote_minimum'].includes(input.operation);
    const cachedConnection=cache.view(cacheOnly||background?0:input.minSlot,cacheOnly,
      {wait,readCategory:background?'warm':'demand',primeReads:input.operation==='quote_warm',trace:row=>input.traceAccounts(row),blockhashCache:blocks,sharedKeys:createFast.hint(input)?createFast.shared(input):[]});
    const started=performance.now(),calls={};
    const connection=new Proxy(cachedConnection,{get(target,key){
      const value=target[key];
      if(typeof value!=='function')return value;
      return async(...args)=>{const began=performance.now();
        if(stages.has(key))progress(key);
        try{return await value.apply(target,args);}
        finally{if(stages.has(key))progress(key,'end');const row=calls[key]||(calls[key]={count:0,ms:0});row.count++;row.ms+=performance.now()-began;}
      };
    }});
    input.waitForBackground=wait;input.background=!!background;
    input.onRecipe=(quoteMint,recipe)=>send({id,recipe,quoteMint});
    let result;
    if(input.operation==='bootstrap'){
      await wait();await blocks.refresh();
      await cache.primeShared(createFast.common,wait);
      if(input.marketCapEnabled)await connection.getMultipleAccountsInfo(ORACLES.map(x=>x.address));
      result={warmed:!!blocks.row};
    }else if(input.operation==='shared_warm'){
      await cache.primeShared(createFast.shared(input),wait);result={warmed:true};
    }else if(input.operation==='quote_warm'){
      result=await require('./quote-warm.cjs').warm(input,connection);
    }else if(input.operation==='market_cap'){
      result=await marketCap(input,connection);
    }else if(input.operation==='quote_minimum'){
      result=await quoteMinimumUsd(input,connection);
    }else if(['quote_limit','warm_limit'].includes(input.operation)){
      result=await quoteMaximumUsd(input,connection);
    }else if(input.route==='prime'){
      await connection.getMultipleAccountsInfo(input.primeAccounts.map(k=>new web3.PublicKey(k)));
      result={warmed:true};
    }else {progress('build');result=await build(input,connection);}
    // Successful simulation still checks the original source slot. A socket
    // outage alone must not discard that completed unsigned transaction.
    if(cache.closed||executionGeneration!==cache.executionGeneration
      ||(input.operation!=='build'&&generation!==cache.generation))throw Error('cache-disconnected');
    cacheLifecycle();
    if(input.operation==='build')result.buildDiagnostics=require('./build-diagnostics.cjs').snapshot(input);
    if(input.operation==='build')result.buildTiming={totalMs:Math.round((performance.now()-started)*1000)/1000,
      createFast:!!createFast.hint(input),localCreate:result.localCreate,
      rpc:require('./catchup-budget.cjs').transport(),calls:Object.fromEntries(Object.entries(calls).map(([key,row])=>
        [key,{count:row.count,ms:Math.round(row.ms*1000)/1000}]))};
    progress('ready');
    send({id,result,stats:cacheStats()});
  }catch(e){cacheLifecycle();send({id,error:REASONS.has(e.message)?e.message:'sdk-or-rpc-failed',
    ...(e.rpcDetails?{rpcDetails:e.rpcDetails}:{}),
    ...(e.simulationDetails?{simulationDetails:e.simulationDetails}:{}),
    buildDiagnostics:require('./build-diagnostics.cjs').snapshot(input),
    ...require('./risk.cjs').feeDetails(e),
    ...(e.message==='jupiter-rate-limited'?{retryAfter:e.retryAfter}:{})});}
  finally{if(foreground)gate.leave();}
}
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
lines.on('line',line=>{
  if(line.length>100000){process.exitCode=1;lines.close();return;}
  try{const message=JSON.parse(line);
    void withReporter(send,()=>require('./catchup-budget.cjs').withCatchupBudget(message.input,()=>handle(message)));
  }catch{process.exitCode=1;lines.close();}
});
lines.on('close',()=>{clearInterval(statsTimer);blocks?.close();cache?.close();process.exit();});

// Emitted only after synchronous SDK imports and the input handler are ready.
send({worker:'ready',boot_ms:Math.round((performance.now()-bootStarted)*1000)/1000});
