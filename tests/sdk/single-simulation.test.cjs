const {test}=require('node:test');
const assert=require('node:assert/strict');
const web3=require('@solana/web3.js');
const {finish}=require('../../trade_execution/sdk/build.cjs');

test('single SDK simulation preserves full plan and returns slot evidence',async()=>{
  const user=web3.Keypair.generate().publicKey, recipient=web3.Keypair.generate().publicKey;
  const input={lookupTables:[],minSlot:100,commitment:'processed'};
  let calls=0,checked=false;
  const connection={
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:200};},
    async simulateTransaction(tx,options){
      calls++;assert.equal(options.minContextSlot,100);assert.equal(options.commitment,'processed');
      assert.equal(options.sigVerify,false);assert.deepEqual(options.accounts.addresses,['account']);
      return {context:{slot:101},value:{err:null,unitsConsumed:40000,accounts:['snapshot']}};
    },
  };
  const ix=web3.SystemProgram.transfer({fromPubkey:user,toPubkey:recipient,lamports:1000});
  const result=await finish(connection,input,user,[ix],{addresses:['account'],verify(rows){assert.deepEqual(rows,['snapshot']);checked=true;}});
  assert.equal(calls,1);assert(checked);assert.equal(result.units,48000);
  assert.equal(result.simulation.slot,101);assert.equal(result.simulation.err,null);
  const before=web3.VersionedTransaction.deserialize(Buffer.from(result.simulation.transaction,'base64'));
  const after=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  assert.equal(before.message.compiledInstructions[0].data[0],2);
  assert.equal(Buffer.from(before.message.compiledInstructions[0].data).readUInt32LE(1),1400000);
  assert.equal(Buffer.from(after.message.compiledInstructions[0].data).readUInt32LE(1),48000);
  assert.deepEqual(before.message.compiledInstructions.slice(1),after.message.compiledInstructions.slice(1));
  assert.deepEqual(before.message.staticAccountKeys,after.message.staticAccountKeys);
  assert.equal(before.message.recentBlockhash,after.message.recentBlockhash);
  assert.deepEqual(before.message.addressTableLookups,after.message.addressTableLookups);
  await assert.rejects(finish(connection,input,user,[ix],{addresses:['account'],verify(){throw Error('pump-fill-rejected');}}),/pump-fill-rejected/);
  connection.simulateTransaction=async()=>({context:{slot:101},value:{err:'failed',unitsConsumed:40000}});
  await assert.rejects(finish(connection,input,user,[ix]),/simulation-rejected/);
});
