'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {executionFetch,MAINNET}=require('../../trade_execution/sdk/execution-rpc.cjs');
const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
const {connectionFor}=require('../../trade_execution/sdk/build.cjs');
const {withReporter}=require('../../trade_execution/sdk/public-rpc.cjs');
const ok=result=>new Response(JSON.stringify({jsonrpc:'2.0',id:1,result}));
const input=()=>({method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,
  method:'simulateTransaction',params:['unsigned',{commitment:'processed',minContextSlot:100,sigVerify:false}]})});

test('SDK fallback preserves request and bounds 429 retries by endpoint cooldown',async()=>{
  const calls=[],events=[];
  const fetcher=async(url,options)=>{
    const body=JSON.parse(options.body);calls.push({url,body});
    assert(options.signal instanceof AbortSignal);
    if(body.method==='getGenesisHash')return ok(MAINNET);
    if(url==='https://public')return new Response('private error',{status:429,headers:{'Retry-After':'30'}});
    return ok({context:{slot:100},value:{err:null}});
  };
  const read=executionFetch({primary:'https://public',fallback:'https://backup/secret',event:e=>events.push(e)},fetcher);
  assert.equal((await (await read('ignored',input())).json()).result.value.err,null);
  await read('ignored',input());
  const primary=calls.filter(c=>c.url==='https://public'&&c.body.method==='simulateTransaction');
  const backup=calls.filter(c=>c.url.includes('backup')&&c.body.method==='simulateTransaction');
  assert.equal(primary.length,1);assert.equal(backup.length,2);
  assert.deepEqual(primary[0].body,backup[0].body);
  assert(!JSON.stringify(events).includes('secret'));
});

test('SDK network timeout, invalid JSON and stale context use backup',async()=>{
  for(const failure of ['timeout','invalid','stale']){
    const urls=[];
    const read=executionFetch({primary:'https://public',fallback:'https://backup'},async(url,options)=>{
      const body=JSON.parse(options.body);
      if(body.method==='getGenesisHash')return ok(MAINNET);
      urls.push(url);
      if(url==='https://public'){
        if(failure==='timeout')throw new DOMException('private URL','TimeoutError');
        if(failure==='invalid')return new Response('private URL');
        return ok({context:{slot:99},value:{err:null}});
      }
      return ok({context:{slot:100},value:{err:null}});
    });
    assert.equal((await (await read('',input())).json()).result.context.slot,100);
    assert.deepEqual(urls,['https://public','https://backup']);
  }
});

test('SDK never masks a valid simulation failure with backup success',async()=>{
  const urls=[];
  const read=executionFetch({primary:'https://public',fallback:'https://backup'},async(url,options)=>{
    const body=JSON.parse(options.body);urls.push(url);
    return ok(body.method==='getGenesisHash'?MAINNET:{context:{slot:100},value:{err:{InstructionError:[1,'Custom']}}});
  });
  assert((await (await read('',input())).json()).result.value.err);
  assert(urls.every(url=>url==='https://public'));
});

test('SDK rejects wrong-chain provider and both-failed errors expose no credentials',async()=>{
  const calls=[],events=[];
  const read=executionFetch({primary:'https://wrong/secret',fallback:'https://down/secret',event:e=>events.push(e)},async(url,options)=>{
    calls.push(JSON.parse(options.body).method);
    if(url.includes('wrong'))return ok('devnet');
    throw Error('provider URL https://down/secret');
  });
  await assert.rejects(read('',input()),/^Error: execution-rpc-unavailable$/);
  assert.deepEqual(calls,['getGenesisHash','getGenesisHash']);
  assert.deepEqual(events,[{event:'execution-rpc-unavailable',method:'simulateTransaction'}]);
});

test('SDK cannot broadcast or sign via fallback transport',async()=>{
  let called=false;
  const read=executionFetch({primary:'https://public'},async()=>{called=true;});
  await assert.rejects(read('',{body:JSON.stringify({method:'sendTransaction',params:['signed']})}),/read-only/);
  assert.equal(called,false);
});

test('empty blockhash cache after websocket loss can obtain a fresh HTTP hash',async()=>{
  const calls=[];
  const rpc={getLatestBlockhash:async config=>{calls.push(config);return {blockhash:'fresh',lastValidBlockHeight:200};}};
  const cache=new AccountCache(rpc);
  const view=cache.view(100,false,{blockhashCache:{pick(){throw Error('blockhash-cache-miss');}}});
  assert.deepEqual(await view.getLatestBlockhash(),{blockhash:'fresh',lastValidBlockHeight:200});
  assert.deepEqual(calls,[{minContextSlot:100,commitment:'confirmed'}]);
  cache.close();
});

test('actual web3 connection parses managed fallback and preserves context',async t=>{
  const calls=[],events=[];
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=JSON.parse(options.body);calls.push({url,body});
    if(body.method!=='getGenesisHash'&&String(url).includes('primary'))
      return new Response('',{status:429});
    return new Response(JSON.stringify({jsonrpc:'2.0',id:body.id,result:body.method==='getGenesisHash'?MAINNET:
      {context:{slot:123},value:{blockhash:MAINNET,lastValidBlockHeight:500}}}));
  });
  await withReporter(e=>events.push(e),async()=>{
    const rpc=connectionFor({rpc:'http://primary.example',rpcFallback:'http://backup.example',
      rpcTimeoutMs:500,commitment:'processed',minSlot:123,lookupTables:[]});
    assert.deepEqual(await rpc.getLatestBlockhash(),{blockhash:MAINNET,lastValidBlockHeight:500});
  });
  const reads=calls.filter(c=>c.body.method==='getLatestBlockhash');
  assert.equal(reads.length,2);assert.deepEqual(reads[0].body,reads[1].body);
  assert.deepEqual(reads[0].body.params,[{commitment:'processed',minContextSlot:123}]);
  assert(events.some(e=>e.event==='public-rpc-429'&&e.fallbackManaged===true));
  assert(!events.some(e=>e.event==='execution-rpc-unavailable'));
});


test('fresh SDK clients share the full Retry-After deadline on disk',async()=>{
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'sol-rpc-cooldown-'));
  let attempts=0;
  const fetcher=async(url,options)=>{
    const method=JSON.parse(options.body).method;
    if(url==='https://public'){
      attempts++;
      return new Response('',{status:429,headers:{'Retry-After':'120'}});
    }
    return ok(method==='getGenesisHash'?MAINNET:{context:{slot:100},value:{err:null}});
  };
  try{
    for(let i=0;i<3;i++){
      const read=executionFetch({primary:'https://public',fallback:'https://backup',cooldownDir:directory},fetcher);
      assert.equal((await (await read('',input())).json()).result.value.err,null);
    }
    assert.equal(attempts,1);
  }finally{
    assert.equal(path.dirname(path.resolve(directory)),path.resolve(os.tmpdir()));
    assert(path.basename(directory).startsWith('sol-rpc-cooldown-'));
    fs.rmSync(directory,{recursive:true,force:true});
  }
});
