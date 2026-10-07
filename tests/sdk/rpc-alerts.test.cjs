const {test}=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {rpcFetch,watchWebsocket,withReporter}=require('../../trade_execution/sdk/public-rpc.cjs');
const {connectionFor}=require('../../trade_execution/sdk/build.cjs');

test('each HTTP 429 reports once without exposing URL, while preserving response',async()=>{
  const events=[],fake=async()=>new Response('private provider text',{status:429});
  const call=rpcFetch((...e)=>events.push(e),fake);
  for(let i=0;i<2;i++){
    const response=await call('https://private/secret',{body:JSON.stringify({method:'getBalance',params:['secret']})});
    assert.equal(response.status,429);assert.equal(await response.text(),'private provider text');
  }
  assert.deepEqual(events,[['getBalance','HTTP'],['getBalance','HTTP']]);
});

test('JSON RPC 429 inspection preserves the SDK body and ignores other errors',async()=>{
  for(const code of [429,'429',-32005]){
    const events=[],body=JSON.stringify({error:{code,message:'secret'}});
    const response=await rpcFetch((...e)=>events.push(e),async()=>new Response(body))('http://local',{body:'{"method":"getAccountInfo"}'});
    assert.equal(await response.text(),body);
    assert.equal(events.length,code===-32005?0:1);
  }
});

test('public WS handshake and subscription 429s report individually',async()=>{
  const ws=new EventEmitter(),events=[];
  ws.call=async()=>{throw {code:429,message:'secret'};};
  watchWebsocket(ws,(...e)=>events.push(e));
  ws.emit('error',new Error('Unexpected server response: 429'));
  await assert.rejects(ws.call('accountSubscribe'),e=>e.code===429);
  ws.emit('error',new Error('connection closed'));
  assert.deepEqual(events,[['connect','WS-handshake'],['accountSubscribe','WS-RPC']]);
});

test('actual SDK connection uses shared HTTP observer',async t=>{
  const events=[];
  t.mock.method(globalThis,'fetch',async()=>new Response('',{status:429}));
  await withReporter(e=>events.push(e),async()=>{
    const rpc=connectionFor({rpc:'http://localhost:8899',minSlot:0,lookupTables:[]});
    await assert.rejects(rpc.getLatestBlockhash());
  });
  assert.deepEqual(events,[{event:'public-rpc-429',method:'getLatestBlockhash',transport:'HTTP'}]);
});
