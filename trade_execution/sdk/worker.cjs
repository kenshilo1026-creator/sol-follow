'use strict';
// Persistent public state and unsigned builds. No key loading or broadcast.
const readline=require('node:readline'),web3=require('@solana/web3.js');
const {build,connectionFor,REASONS}=require('./build.cjs');
const {AccountCache}=require('./account-cache.cjs');
const {BlockhashCache}=require('./blockhash-cache.cjs');
const {BackgroundGate}=require('./background-gate.cjs');
const {quoteLimit}=require('./observed-buy.cjs');
const {withReporter}=require('./public-rpc.cjs');
const gate=new BackgroundGate();
let cache,blocks,rpcUrl,cacheGeneration;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
async function handle(message){
  const {id,input}=message;
  const background=input.prewarm||['warm_limit','bootstrap'].includes(input.operation);
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
    const connection=cache.view(input.operation==='quote_limit'?0:input.minSlot,input.operation==='quote_limit',
      {wait,blockhashCache:blocks});
    input.waitForBackground=wait;input.background=!!background;
    input.onRecipe=(quoteMint,recipe)=>send({id,recipe,quoteMint});
    let result;
    if(input.operation==='bootstrap'){
      await wait();await blocks.refresh();result={warmed:!!blocks.row};
    }else if(['quote_limit','warm_limit'].includes(input.operation)){
      result=await quoteLimit(input,connection);
    }else if(input.route==='prime'){
      await connection.getMultipleAccountsInfo(input.primeAccounts.map(k=>new web3.PublicKey(k)));
      result={warmed:true};
    }else result=await build(input,connection);
    if(generation!==cache.generation)throw Error('cache-disconnected');
    send({id,result,stats:{hits:cache.hits,misses:cache.misses,accounts:cache.rows.size,
      blockhashHits:blocks.hits,blockhashMisses:blocks.misses}});
  }catch(e){send({id,error:REASONS.has(e.message)?e.message:'sdk-or-rpc-failed',
    ...(e.message==='jupiter-rate-limited'?{retryAfter:e.retryAfter}:{})});}
  finally{if(foreground)gate.leave();}
}
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
lines.on('line',line=>{
  if(line.length>100000){process.exitCode=1;lines.close();return;}
  try{void withReporter(send,()=>handle(JSON.parse(line)));}catch{process.exitCode=1;lines.close();}
});
lines.on('close',()=>{blocks?.close();cache?.close();process.exit();});
