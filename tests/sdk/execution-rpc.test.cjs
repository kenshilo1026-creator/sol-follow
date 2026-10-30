'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {executionFetch,MAINNET}=require('../../trade_execution/sdk/execution-rpc.cjs');
const {AccountCache}=require('../../trade_execution/sdk/account-cache.cjs');
const {connectionFor}=require('../../trade_execution/sdk/build.cjs');
const {withReporter}=require('../../trade_execution/sdk/public-rpc.cjs');
const {withCatchupBudget,catchupPolicy}=require('../../trade_execution/sdk/catchup-budget.cjs');
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
  assert.equal(events.length,1);
  assert.equal(events[0].method,'simulateTransaction');
  assert.deepEqual(events[0].failures.map(row=>row.reason),['execution-genesis-unavailable','execution-network-failed']);
  assert(!JSON.stringify(events).includes('secret'));
});

test('fresh-slot account read recovers while backup is cooling, without changing request',async()=>{
  const calls=[];let primaryReads=0;
  const read=executionFetch({primary:'https://sol.alchemy.com/private',fallback:'https://backup/private'},async(url,options)=>{
    const body=JSON.parse(options.body);calls.push({url,body});
    if(url.includes('backup'))return new Response('',{status:429});
    if(body.method==='getGenesisHash')return ok(MAINNET);
    if(++primaryReads===1)return new Response(JSON.stringify({error:{code:-32016,data:{contextSlot:99}}}));
    return ok({context:{slot:100},value:[]});
  });
  const options={body:JSON.stringify({method:'getMultipleAccounts',params:[[],{commitment:'processed',minContextSlot:100}]})};
  assert.deepEqual((await (await read('',options)).json()).result.value,[]);
  assert.equal(primaryReads,2);
  assert.equal(calls.filter(c=>c.url.includes('backup')).length,1);
  const reads=calls.filter(c=>c.body.method==='getMultipleAccounts');
  assert.deepEqual(reads[0].body,reads[1].body);
});

test('persistent slot lag retries at most twice and carries safe details through real web3',async t=>{
  let reads=0;const requests=[];
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=JSON.parse(options.body);
    if(body.method==='getGenesisHash')return ok(MAINNET);
    reads++;requests.push(body);
    return new Response(JSON.stringify({error:{code:-32016,message:'private URL secret',data:{contextSlot:99}}}));
  });
  const rpc=connectionFor({rpc:'https://sol.alchemy.com/secret',rpcTimeoutMs:4000,
    commitment:'processed',minSlot:100,lookupTables:[]});
  await assert.rejects(rpc.getMultipleAccountsInfo([]),error=>{
    assert.equal(error.message,'execution-context-behind');
    assert.equal(error.rpcDetails.method,'getMultipleAccounts');
    assert.equal(error.rpcDetails.failures.length,3);
    assert(error.rpcDetails.failures.every(row=>row.required_slot===100&&row.context_slot===99));
    assert(!JSON.stringify(error).includes('secret'));
    return true;
  });
  assert.equal(reads,3);
  assert(requests.every(body=>JSON.stringify(body)===JSON.stringify(requests[0])));
  assert(require('../../trade_execution/sdk/build.cjs').REASONS.has('execution-context-behind'));
});

test('catch-up retry has a shared time budget and observes cancellation',async()=>{
  for(const cancel of [false,true]){
    let reads=0;const controller=new AbortController();
    const read=executionFetch({primary:'https://rpc',timeoutMs:4000},async(url,options)=>{
      if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
      if(++reads===1)return ok({context:{slot:99},value:{err:null}});
      if(cancel)controller.abort();
      return new Promise((resolve,reject)=>{
        if(options.signal.aborted)return reject(options.signal.reason);
        options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});
      });
    });
    // Keep Node alive while the fake fetch waits on AbortSignal.timeout (unref'd).
    const keepAlive=setTimeout(()=>{},1500),start=performance.now();
    try{
      await assert.rejects(read('',{...input(),signal:controller.signal}),error=>{
        assert.equal(error.message,cancel?'execution-request-aborted':'execution-rpc-unavailable');
        if(!cancel)assert.equal(error.rpcDetails.failures.at(-1).reason,'execution-timeout');
        return true;
      });
      assert.equal(reads,2);
      assert(performance.now()-start<1000);
    }finally{clearTimeout(keepAlive);}
  }
});

test('429 and deterministic RPC failures never enter catch-up retries',async()=>{
  for(const code of [429,-32602,-32002]){
    let reads=0;
    const read=executionFetch({primary:'https://rpc'},async(url,options)=>{
      if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
      reads++;
      return new Response(JSON.stringify({error:{code,message:'private response'}}));
    });
    if(code===429)await assert.rejects(read('',input()),/execution-rpc-unavailable/);
    else assert.equal((await (await read('',input())).json()).error.code,code);
    assert.equal(reads,1);
  }
});

test('buy can catch up after the old 250ms window with backup cooling',async()=>{
  let firstRead,reads=0,backupReads=0;const bodies=[];
  const read=executionFetch({primary:'https://sol.alchemy.com/key',fallback:'https://backup'},async(url,options)=>{
    const body=JSON.parse(options.body);
    if(url.includes('backup')){backupReads++;return new Response('',{status:429});}
    if(body.method==='getGenesisHash')return ok(MAINNET);
    firstRead??=performance.now();reads++;bodies.push(options.body);
    if(performance.now()-firstRead<280)
      return new Response(JSON.stringify({error:{code:-32016,data:{contextSlot:99}}}));
    return ok({context:{slot:100},value:{err:null}});
  });
  const result=await withCatchupBudget({buySignalExpiresAtMs:Date.now()+2000},()=>read('',input()));
  assert.equal((await result.json()).result.context.slot,100);
  assert(reads>=4&&reads<=9);assert.equal(backupReads,1);
  assert(bodies.every(body=>body===bodies[0]));
});

test('buy catch-up window is shared across RPCs and isolated between concurrent builds',async()=>{
  const expiring={buySignalExpiresAtMs:Date.now()+100};
  const fresh={buySignalExpiresAtMs:Date.now()+3000};
  await Promise.all([
    withCatchupBudget(expiring,async()=>{
      const policy=catchupPolicy();
      assert(policy.deadline<=performance.now());
      await new Promise(resolve=>setTimeout(resolve,10));
      assert.equal(catchupPolicy().deadline,policy.deadline);
    }),
    withCatchupBudget(fresh,async()=>{
      const policy=catchupPolicy();
      assert(policy.deadline-performance.now()>900);
      await new Promise(resolve=>setTimeout(resolve,25));
      assert.equal(catchupPolicy().deadline,policy.deadline);
    }),
  ]);
  assert.equal(catchupPolicy().rounds,2); // Background/sell callers retain the short policy.
});

test('expired or near-expiry buy does not keep polling a lagging node',async()=>{
  for(const remaining of [-1000,100]){
    let reads=0;
    const read=executionFetch({primary:'https://rpc'},async(url,options)=>{
      if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
      reads++;return new Response(JSON.stringify({error:{code:-32016,data:{contextSlot:99}}}));
    });
    await assert.rejects(withCatchupBudget({buySignalExpiresAtMs:Date.now()+remaining},()=>read('',input())),
      /execution-context-behind/);
    assert.equal(reads,1);
  }
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


test('WS winner cancellation never cools the endpoint or aborts another build',async()=>{
  const {withReadSignal}=require('../../trade_execution/sdk/read-cancel.cjs');
  const controller=new AbortController();let started,reads=0;
  const ready=new Promise(resolve=>{started=resolve;});
  const read=executionFetch({primary:'https://rpc'},async(url,options)=>{
    if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
    if(++reads===1){started();return new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});
    });}
    return ok({context:{slot:100},value:{err:null}});
  });
  const first=withReadSignal(controller.signal,()=>read('',input()));
  const rejected=assert.rejects(first,/execution-request-aborted/);
  await ready;controller.abort();await rejected;
  assert.equal((await (await read('',input())).json()).result.context.slot,100);
  assert.equal(reads,2);
});


const buy=work=>withCatchupBudget({buySignalExpiresAtMs:Date.now()+3000},work);
const stalled=signal=>new Promise((resolve,reject)=>{
  if(signal.aborted)return reject(signal.reason);
  signal.addEventListener('abort',()=>reject(signal.reason),{once:true});
});

test('hedged buy simulation cancels slow primary without cooling it and keeps identical body',async()=>{
  const calls=[];let slow=true,cancelled=false;
  const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
    if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
    calls.push([url,options.body]);
    if(url==='https://primary'&&slow){
      try{return await stalled(options.signal);}finally{cancelled=true;}
    }
    return ok({context:{slot:100},value:{err:null}});
  });
  await buy(async()=>{
    assert.equal((await (await read('',input())).json()).result.value.err,null);
    assert(require('../../trade_execution/sdk/catchup-budget.cjs').transport().some(row=>
      row.provider==='fallback'&&row.hedged&&row.outcome==='response'));
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert(cancelled);assert.deepEqual(calls.map(c=>c[0]),['https://primary','https://backup']);
  assert.equal(calls[0][1],calls[1][1]);
  slow=false;calls.length=0;
  await buy(()=>read('',input()));
  assert.deepEqual(calls.map(c=>c[0]),['https://primary']);
});

test('fast valid failure is final, without speculative backup or failure shopping',async()=>{
  for(const rpcError of [false,true]){
    const calls=[];
    const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
      if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
      calls.push(url);
      return rpcError?new Response(JSON.stringify({error:{code:-32002}})):
        ok({context:{slot:100},value:{err:{InstructionError:[1,'Custom']}}});
    });
    const body=await (await buy(()=>read('',input()))).json();
    assert(rpcError?body.error:body.result.value.err);
    assert.deepEqual(calls,['https://primary']);
  }
});

test('a racing backup simulation failure remains a failure and stops the slow primary',async()=>{
  const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
    if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
    if(url==='https://primary')return stalled(options.signal);
    return ok({context:{slot:100},value:{err:{InstructionError:[1,'Custom']}}});
  });
  const body=await (await buy(()=>read('',input()))).json();
  assert(body.result.value.err);
});

test('invalid speculative backup cannot replace a valid in-flight primary',async()=>{
  let release;const ready=new Promise(resolve=>{release=resolve;});
  const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
    if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
    if(url==='https://primary'){await ready;return ok({context:{slot:100},value:{err:null}});}
    release();return ok({context:{slot:99},value:{err:null}});
  });
  assert.equal((await (await buy(()=>read('',input()))).json()).result.context.slot,100);
});

test('hedge root cancellation aborts both simulations and cleans up',async()=>{
  const root=new AbortController();let started=0,ended=0;
  const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
    if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
    if(++started===2)root.abort();
    try{return await stalled(options.signal);}finally{ended++;}
  });
  await assert.rejects(buy(()=>read('',{...input(),signal:root.signal})),/execution-request-aborted/);
  assert.equal(started,2);assert.equal(ended,2);
});

test('read-only background calls and disabled hedge retain sequential RPC policy',async()=>{
  for(const mode of ['disabled','background','account']){
    let active=0,overlap=false;const calls=[];
    const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:mode==='disabled'?0:5},async(url,options)=>{
      if(JSON.parse(options.body).method==='getGenesisHash')return ok(MAINNET);
      calls.push(url);overlap ||= active>0;active++;
      await new Promise(resolve=>setTimeout(resolve,20));active--;
      return ok({context:{slot:100},value:mode==='account'?[]:{err:null}});
    });
    const options=mode==='account'?{...input(),body:JSON.stringify({method:'getMultipleAccounts',params:[[],{minContextSlot:100}]})}:input();
    await (mode==='background'?read('',options):buy(()=>read('',options)));
    assert.deepEqual(calls,['https://primary']);assert(!overlap);
  }
});

test('hedge respects wrong-chain and rate-limit rejection without polling cooling providers',async()=>{
  for(const rejected of ['genesis','429']){
    let primaryReads=0;
    const read=executionFetch({primary:'https://primary',fallback:'https://backup',hedgeMs:5},async(url,options)=>{
      const genesis=JSON.parse(options.body).method==='getGenesisHash';
      if(url==='https://primary'){
        if(genesis&&rejected==='genesis')return ok('wrong-chain');
        if(!genesis){primaryReads++;return new Response('',{status:429});}
      }
      return ok(genesis?MAINNET:{context:{slot:100},value:{err:null}});
    });
    for(let i=0;i<2;i++)assert.equal((await (await buy(()=>read('',input()))).json()).result.value.err,null);
    assert.equal(primaryReads,rejected==='429'?1:0);
  }
});
