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


for(const side of ['buy','sell','sweep'])test(`fixed total priority fee: ${side}, one simulation`,async()=>{
  const user=web3.Keypair.generate().publicKey,recipient=web3.Keypair.generate().publicKey;
  const input={side,lookupTables:[],minSlot:100,commitment:'processed',priorityFeeLamports:'200000'};
  let calls=0;
  const connection={async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:200};},
    async simulateTransaction(tx){calls++;
      const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
      assert.equal(ixs[1].data[0],3);
      const simulatedFee=(ixs[1].data.readBigUInt64LE(1)*1400000n+999999n)/1000000n;
      assert(simulatedFee>=200000n&&simulatedFee<=200001n);
      return {context:{slot:100},value:{err:null,unitsConsumed:94460}};}};
  const result=await finish(connection,input,user,[web3.SystemProgram.transfer({fromPubkey:user,toPubkey:recipient,lamports:1})]);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
  const limit=ixs[0].data.readUInt32LE(1),price=ixs[1].data.readBigUInt64LE(1);
  assert.equal(limit,113352);assert.equal((price*BigInt(limit)+999999n)/1000000n,200000n);
  assert.equal(result.priorityFee.chargedLamports,'200000');
  assert.equal(result.simulation.version,2);assert.equal(calls,1);
});

test('priority-fee integer math stays within total budget at all supported CU limits',()=>{
  const fee=require('../../trade_execution/sdk/priority-fee.cjs');
  for(const budget of [0n,1n,200000n,1000000n])for(const units of [1,55338,113352,1000000,1399999,1400000]){
    const price=fee.price(budget,units),actual=(price*BigInt(units)+999999n)/1000000n;
    assert(actual<=budget);assert(budget-actual<=1n);
  }
  for(const value of ['-1','0.1','NaN','18446744073709551616',200000,null])
    assert.throws(()=>fee.budget({priorityFeeLamports:value}),/invalid-priority-fee/);
});
