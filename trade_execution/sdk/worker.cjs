'use strict';
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
async function handle(message){
  const {id,input}=message;
  const background=input.prewarm||['warm_limit','bootstrap','shared_warm'].includes(input.operation);
  const foreground=input.operation!=='priority'&&!background;
  if(foreground)gate.enter();
  try{
    if(input.activeTrades!==undefined)gate.setTrades(input.activeTrades);
    if(input.operation==='priority'){gate.setTrades(input.active);send({id,result:{priority:input.active}});return;}
    if(!cache){
      rpcUrl=input.rpc;
      const rpc=connectionFor({...input,minSlot:0});
      cache=new AccountCache(rpc,{...input.cache,commitment:input.commitment||'confirmed'});
      blocks=new BlockhashCache(rpc,{...input.blockhashCache,commitment:input.commitment||'confirmed',
        isBusy:()=>gate.busy,head:()=>cache.head});
      cache.onInvalidate=()=>blocks.invalidate();
    }
    if(input.rpc!==rpcUrl)throw Error('sdk-or-rpc-failed');
    if(!Number.isSafeInteger(input.minSlot)||input.minSlot<0)throw Error('invalid-slot');
    if(cacheGeneration!==input.cacheGeneration){cache.invalidate();cacheGeneration=input.cacheGeneration;}
    const generation=cache.generation;
    const wait=background?()=>gate.wait():async()=>{};
    const cacheOnly=['quote_limit','quote_minimum'].includes(input.operation);
    const cachedConnection=cache.view(cacheOnly?0:input.minSlot,cacheOnly,
      {wait,blockhashCache:blocks,sharedKeys:createFast.hint(input)?createFast.shared(input):[]});
    const started=performance.now(),calls={};
    const connection=new Proxy(cachedConnection,{get(target,key){
      const value=target[key];
      if(typeof value!=='function')return value;
      return async(...args)=>{const began=performance.now();
        try{return await value.apply(target,args);}
        finally{const row=calls[key]||(calls[key]={count:0,ms:0});row.count++;row.ms+=performance.now()-began;}
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
    }else if(input.operation==='market_cap'){
      result=await marketCap(input,connection);
    }else if(input.operation==='quote_minimum'){
      result=await quoteMinimumUsd(input,connection);
    }else if(['quote_limit','warm_limit'].includes(input.operation)){
      result=await quoteMaximumUsd(input,connection);
    }else if(input.route==='prime'){
      await connection.getMultipleAccountsInfo(input.primeAccounts.map(k=>new web3.PublicKey(k)));
      result={warmed:true};
    }else result=await build(input,connection);
    if(generation!==cache.generation)throw Error('cache-disconnected');
    if(input.operation==='build')result.buildTiming={totalMs:Math.round((performance.now()-started)*1000)/1000,
      createFast:!!createFast.hint(input),calls:Object.fromEntries(Object.entries(calls).map(([key,row])=>
        [key,{count:row.count,ms:Math.round(row.ms*1000)/1000}]))};
    send({id,result,stats:{hits:cache.hits,misses:cache.misses,accounts:cache.rows.size,wsReadWins:cache.wsReadWins,
      blockhashHits:blocks.hits,blockhashMisses:blocks.misses}});
  }catch(e){send({id,error:REASONS.has(e.message)?e.message:'sdk-or-rpc-failed',
    ...(e.rpcDetails?{rpcDetails:e.rpcDetails}:{}),
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
lines.on('close',()=>{blocks?.close();cache?.close();process.exit();});
