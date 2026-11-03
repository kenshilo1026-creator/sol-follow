'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),readline=require('node:readline');
const path=require('node:path');
test('worker progress precedes final response and carries only fixed diagnostic fields',async t=>{
  const child=spawn(process.execPath,[path.resolve(__dirname,'../../trade_execution/sdk/worker.cjs')],
    {stdio:['pipe','pipe','ignore']});
  const lines=readline.createInterface({input:child.stdout});
  t.after(()=>{lines.close();child.kill();});
  const received=[];let ready;
  const result=new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('worker-test-timeout')),10000);
    t.after(()=>clearTimeout(timer));
    lines.on('line',line=>{const row=JSON.parse(line);
      if(row.worker==='ready'){ready=row;return;}
      if(row.id!==1)return;
      assert(ready,'SDK ready must precede handling a build request');
      received.push(row);if(row.error||row.result)resolve(row);});
    child.once('error',reject);
  });
  child.stdin.write(JSON.stringify({id:1,input:{operation:'build',route:'unknown',minSlot:0,lookupTables:[],
    rpc:'http://127.0.0.1:1',ws:'ws://127.0.0.1:1',commitment:'processed',activeTrades:1}})+'\n');
  const failure=await result;
  assert.equal(failure.error,'unknown-route');
  assert.equal(failure.buildDiagnostics.source_slot,0);
  assert.deepEqual(failure.buildDiagnostics.reads,[]);
  assert(!JSON.stringify(failure.buildDiagnostics).includes('127.0.0.1'));
  assert.deepEqual(Object.keys(ready).sort(),['boot_ms','worker']);
  assert(Number.isFinite(ready.boot_ms)&&ready.boot_ms>=0);
  assert.deepEqual(received.filter(r=>r.progress).map(r=>r.progress.stage),['received','build']);
  for(const row of received.filter(r=>r.progress)){
    assert.deepEqual(Object.keys(row.progress).sort(),['ms','phase','stage']);
    assert(row.progress.ms>=0);
  }
});
