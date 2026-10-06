'use strict';
// JSON-lines worker: public state only; never receives a private key or signs.
const readline=require('node:readline');
const web3=require('@solana/web3.js');
const {build,connectionFor,REASONS}=require('./build.cjs');
const {AccountCache}=require('./account-cache.cjs');
let cache,rpcUrl;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
async function handle(message){
  const {id,input}=message;
  try{
    if(!cache){
      rpcUrl=input.rpc;
      cache=new AccountCache(connectionFor({...input,minSlot:0}),input.cache);
    }
    if(input.rpc!==rpcUrl)throw Error('sdk-or-rpc-failed');
    if(!Number.isSafeInteger(input.minSlot)||input.minSlot<0)throw Error('invalid-slot');
    const connection=cache.view(input.minSlot);
    input.onRecipe=(quoteMint,recipe)=>send({id,recipe,quoteMint});
    let result;
    if(input.route==='prime'){
      await connection.getMultipleAccountsInfo(input.primeAccounts.map(k=>new web3.PublicKey(k)));
      result={warmed:true};
    }else result=await build(input,connection);
    send({id,result,stats:{hits:cache.hits,misses:cache.misses,accounts:cache.rows.size}});
  }catch(e){send({id,error:REASONS.has(e.message)?e.message:'sdk-or-rpc-failed',
    ...(e.message==='jupiter-rate-limited'?{retryAfter:e.retryAfter}:{})});}
}
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
lines.on('line',line=>{
  if(line.length>100000){process.exitCode=1;lines.close();return;}
  try{void handle(JSON.parse(line));}catch{process.exitCode=1;lines.close();}
});
lines.on('close',()=>{cache?.close();process.exit();});
