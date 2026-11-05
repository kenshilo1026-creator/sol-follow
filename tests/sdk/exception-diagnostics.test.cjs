"use strict";
const {test}=require('node:test'),assert=require('node:assert/strict');
const diag=require('../../trade_execution/sdk/exception-diagnostics.cjs');
const {compile,finish}=require('../../trade_execution/sdk/build.cjs');
const web3=require('@solana/web3.js');

test('classification never exports messages, arbitrary codes, names, stacks or causes',()=>{
  const e=new RangeError('https://provider.invalid/PRIVATE_KEY');
  e.code='PRIVATE_KEY';e.name='PRIVATE_KEY';e.cause=Error('PRIVATE_KEY');
  diag.mark(e,'serialize-transaction');diag.mark(e,'build');
  assert.deepEqual(diag.details(e),{category:'range-error',phase:'serialize-transaction'});
  const assertion=Object.assign(Error('secret'),{code:'ERR_ASSERTION'});
  assert.deepEqual(diag.details(assertion),{category:'assertion-error',phase:'build',code:'ERR_ASSERTION'});
  assert.deepEqual(diag.details('PRIVATE_KEY'),{category:'unknown-thrown',phase:'build'});
  assert.deepEqual(diag.details(null),{category:'unknown-thrown',phase:'build'});
});

test('real oversized serialized packet identifies serialization and never reaches simulation',async()=>{
  const user=web3.Keypair.generate().publicKey;
  const ix=new web3.TransactionInstruction({programId:web3.SystemProgram.programId,keys:[],data:Buffer.alloc(3000)});
  let simulationCalls=0;
  const connection={async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:1};},
    async simulateTransaction(){simulationCalls++;throw Error('unexpected');}};
  await assert.rejects(finish(connection,{lookupTables:[],minSlot:1},user,[ix]),e=>{
    assert.equal(diag.details(e).phase,'serialize-transaction');
    assert.equal(diag.details(e).category,'range-error');return true;
  });
  assert.equal(simulationCalls,0);
});

test('lookup failure is distinguishable from a subsequent compilation failure',async()=>{
  const user=web3.Keypair.generate().publicKey;
  await assert.rejects(finish({async getAddressLookupTable(){throw Object.assign(Error('secret'),{code:'ETIMEDOUT'});}},
    {lookupTables:[user.toBase58()]},user,[]),e=>{
      assert.deepEqual(diag.details(e),{category:'error',phase:'lookup-tables',code:'ETIMEDOUT'});return true;
    });
  const keys=Array.from({length:260},()=>({pubkey:web3.Keypair.generate().publicKey,isSigner:false,isWritable:false}));
  const ix=new web3.TransactionInstruction({programId:web3.SystemProgram.programId,keys,data:Buffer.alloc(0)});
  assert.throws(()=>compile(user,web3.PublicKey.default.toBase58(),[ix],[]),e=>{
    assert.equal(diag.details(e).phase,'compile-message');return true;
  });
});
