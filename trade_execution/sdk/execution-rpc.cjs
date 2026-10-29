'use strict';
// Unsigned SDK reads/simulation only. All fallback requests preserve the body,
// including commitment and minContextSlot. Signing/broadcast stays in Python.
const MAINNET='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d';
const {setTimeout:delay}=require('node:timers/promises');
function failure(reason,detail={}){return Object.assign(Error(reason),{rpcFailure:{reason,...detail}});}
function validResult(method,result){
  if(result===null||result===undefined)return false;
  const context=Number.isSafeInteger(result?.context?.slot)&&result.context.slot>=0;
  if(['getAccountInfo','getMultipleAccounts','getLatestBlockhash','simulateTransaction'].includes(method)&&!context)return false;
  if(method==='simulateTransaction')return !!result.value&&Object.hasOwn(result.value,'err');
  if(method==='getLatestBlockhash')return typeof result.value?.blockhash==='string'
    &&Number.isSafeInteger(result.value.lastValidBlockHeight)&&result.value.lastValidBlockHeight>0;
  if(method==='getAccountInfo')return Object.hasOwn(result,'value')
    &&(result.value===null||typeof result.value==='object');
  if(method==='getMultipleAccounts')return Array.isArray(result.value);
  if(method==='getEpochInfo')return Number.isSafeInteger(result.absoluteSlot)&&Number.isSafeInteger(result.epoch);
  return true;
}
function executionFetch({primary,fallback,timeoutMs=4000,cooldownDir,report=()=>{},event=()=>{}},fetcher=fetch){
  const urls=[...new Set([primary,fallback].filter(Boolean))];
  const verified=new Map(),rejected=new Set(),cooldown=new Map();
  const shared=require("./endpoint-cooldown.cjs").sharedCooldown(cooldownDir);
  async function request(url,options,method,timeout=timeoutMs){
    const source=new URL(url).hostname.endsWith('.alchemy.com')?'alchemy-execution':'execution';
    if(Date.now()<Math.max(cooldown.get(url)||0,shared.get(url)))throw Error('execution-endpoint-cooling');
    let response;
    try{response=await fetcher(url,{...options,signal:options.signal?
      AbortSignal.any([options.signal,AbortSignal.timeout(timeout)]):AbortSignal.timeout(timeout)});}
    catch(error){if(!options.signal?.aborted)cooldown.set(url,Date.now()+5000);
      throw failure(error.name==='TimeoutError'?'execution-timeout':'execution-network-failed');}
    if(response.status===429){
      report(method,'HTTP',true,source);
      const seconds=Number(response.headers.get('Retry-After')||10);
      shared.set(url,Date.now()+1000*(Number.isFinite(seconds)?Math.max(1,Math.min(120,seconds)):10));
      cooldown.set(url,Date.now()+1000*(Number.isFinite(seconds)?Math.max(1,Math.min(120,seconds)):10));
    }
    if(!response.ok){
      if(response.status>=500)cooldown.set(url,Date.now()+5000);
      await response.body?.cancel();throw failure(response.status===429?'execution-rate-limited':'execution-http-unavailable',
        {http_status:response.status});
    }
    const text=await response.text();let body;
    try{body=JSON.parse(text);}catch{throw Error('execution-invalid-json');}
    if(body.error&&([429,'429'].includes(body.error.code)||/too many requests|rate limit/i.test(String(body.error.message||'')))){
      report(method,'JSON-RPC',true,source);shared.set(url,Date.now()+10000);cooldown.set(url,Date.now()+10000);
      throw failure('execution-rate-limited',{rpc_code:Number(body.error.code)||429});
    }
    return {body,text};
  }
  async function verify(url,options){
    if(rejected.has(url))throw Error('execution-wrong-chain');
    if(!verified.has(url)){
      const check=(async()=>{
        const {body}=await request(url,{...options,body:JSON.stringify({jsonrpc:'2.0',id:1,method:'getGenesisHash',params:[]})},'getGenesisHash');
        if(body.error||!body.result)throw Error('execution-genesis-unavailable');
        if(typeof body.result!=='string')throw Error('execution-genesis-unavailable');
        try{new (require('@solana/web3.js').PublicKey)(body.result);}catch{throw Error('execution-genesis-unavailable');}
        if(body.result!==MAINNET){rejected.add(url);throw Error('execution-wrong-chain');}
      })();
      verified.set(url,check);
      check.catch(()=>{if(verified.get(url)===check)verified.delete(url);});
    }
    await verified.get(url);
  }
  return async(_url,options)=>{
    const input=JSON.parse(options.body),method=input.method;
    const verificationOptions=options;
    const scoped=require('./read-cancel.cjs').readSignal();
    if(scoped)options={...options,signal:options.signal?AbortSignal.any([options.signal,scoped]):scoped};
    // Never accidentally enable a second broadcast path in an SDK adapter.
    if(typeof method!=='string'||(!method.startsWith('get')&&method!=='simulateTransaction'))
      throw Error('execution-sdk-read-only');
    const minSlot=Math.max(0,...(input.params||[]).filter(v=>v&&typeof v==='object').map(v=>v.minContextSlot||0));
    const failures=[],behind=new Set();
    async function attempt(url,round=0,timeout=timeoutMs){
      try{
        if(options.signal?.aborted)throw Error('execution-request-aborted');
        await verify(url,verificationOptions);
        if(options.signal?.aborted)throw Error('execution-request-aborted');
        const {body,text}=await request(url,options,method,timeout);
        if(body.error){
          // Deterministic request/preflight failures are not availability failures.
          if([-32600,-32601,-32602,-32002].includes(body.error.code))
            {event({event:'execution-rpc-recovered',method});return new Response(text,{headers:{'content-type':'application/json'}});}
          const detail=Number.isSafeInteger(body.error.code)?{rpc_code:body.error.code}:{};
          if(Number.isSafeInteger(body.error.data?.contextSlot))detail.context_slot=body.error.data.contextSlot;
          throw failure(body.error.code===-32016?'execution-context-behind':'execution-rpc-unavailable',detail);
        }
        if(!Object.hasOwn(body,'result')||!validResult(method,body.result))throw Error('execution-result-unavailable');
        if(minSlot&&(!Number.isSafeInteger(body.result?.context?.slot)||body.result.context.slot<minSlot)
            &&['getAccountInfo','getMultipleAccounts','getLatestBlockhash','simulateTransaction'].includes(method))
          throw failure('execution-context-behind',{context_slot:body.result.context.slot});
        if(method==='simulateTransaction'&&(!body.result?.value||!Object.hasOwn(body.result.value,'err')))
          throw Error('execution-invalid-simulation');
        // A successful simulation response with value.err remains a failure;
        // never ask the backup for a more permissive answer.
        event({event:'execution-rpc-recovered',method});
        return new Response(text,{headers:{'content-type':'application/json'}});
      }catch(error){
        if(options.signal?.aborted)throw Error('execution-request-aborted');
        const known=new Set(['execution-endpoint-cooling','execution-invalid-json','execution-wrong-chain',
          'execution-genesis-unavailable','execution-result-unavailable','execution-invalid-simulation']);
        const detail=error.rpcFailure||{reason:known.has(error.message)?error.message:'execution-network-failed'};
        failures.push({provider:url===urls[0]?'primary':'fallback',
          source:new URL(url).hostname.endsWith('.alchemy.com')?'alchemy':'configured',
          attempt:round+1,...detail,...(minSlot?{required_slot:minSlot}:{})});
        if(detail.reason==='execution-context-behind'&&minSlot>0)behind.add(url);else behind.delete(url);
      }
    }
    // Try the backup immediately. Only slot lag warrants a short catch-up window;
    // never retry 429/cooling endpoints or mask deterministic simulation failures.
    for(const url of urls){const result=await attempt(url);if(result)return result;}
    const policy=require('./catchup-budget.cjs').catchupPolicy();
    const deadline=policy.deadline;
    for(let round=1;round<=policy.rounds&&behind.size;round++){
      const pause=policy.pause(round);
      if(performance.now()+pause>=deadline)break;
      await delay(pause,undefined,{signal:options.signal});
      for(const url of [...behind]){
        const remaining=Math.floor(deadline-performance.now());
        if(remaining<=0)break;
        const result=await attempt(url,round,Math.max(1,Math.min(timeoutMs,remaining)));
        if(result)return result;
      }
    }
    event({event:'execution-rpc-unavailable',method,failures});
    throw Object.assign(Error(behind.size?'execution-context-behind':'execution-rpc-unavailable'),
      {rpcDetails:{method,failures}});
  };
}
module.exports={executionFetch,MAINNET};
