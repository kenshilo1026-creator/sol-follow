'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const BN = require('bn.js');
const web3 = require('@solana/web3.js');
const pump = require('@pump-fun/pump-sdk');
const spl = require('@solana/spl-token');
const {build,minimums,exactQuoteInstruction,safeMint,compile} = require('./build.cjs');

test('end-to-end slippage is a single bound, with integer rounding',()=>{
  const {quoteIn,minOut} = minimums(new BN(10000),new BN(100000),'2');
  assert.equal(quoteIn.toString(),'9900');
  assert.equal(minOut.toString(),'98000');
  assert.equal(minimums(new BN(10000),new BN(101),'0.25').minOut.toString(),'101');
  for(const s of ['0','-1','NaN','20.01'])
    assert.throws(()=>minimums(new BN(100),new BN(100),s));
  const huge=new BN('18446744073709551615');
  assert(minimums(huge,huge,'2').minOut.eq(huge.muln(98).addn(99).divn(100)));
});

test('official SDK Pump account metas preserved, exact-in amounts replace buy-v2 args',async()=>{
  const mint=web3.Keypair.generate().publicKey, user=web3.Keypair.generate().publicKey;
  const quoteMint=web3.Keypair.generate().publicKey;
  const ix=await pump.PUMP_SDK.getBuyV2InstructionRaw({mint,user,quoteMint,
    creator:web3.Keypair.generate().publicKey,amount:new BN(1000),quoteAmount:new BN(50),
    tokenProgram:spl.TOKEN_2022_PROGRAM_ID,quoteTokenProgram:spl.TOKEN_2022_PROGRAM_ID});
  const converted=exactQuoteInstruction(ix,new BN(49),new BN(980));
  assert.deepEqual(converted.keys,ix.keys);
  assert.equal(converted.data.subarray(0,8).toString('hex'),'c2ab1c46684d5b2f');
  assert.equal(converted.data.readBigUInt64LE(8),49n);
  assert.equal(converted.data.readBigUInt64LE(16),980n);
  assert.equal(converted.keys[14].pubkey.toBase58(),spl.getAssociatedTokenAddressSync(mint,user,false,spl.TOKEN_2022_PROGRAM_ID).toBase58());
  assert.equal(converted.keys[15].pubkey.toBase58(),spl.getAssociatedTokenAddressSync(quoteMint,user,false,spl.TOKEN_2022_PROGRAM_ID).toBase58());
});

test('transfer-changing mint extensions require a separate adapter',()=>{
  safeMint({isInitialized:true,tlvData:Buffer.alloc(0)});
  for(const type of [1,9,16]) {
    const tlv=Buffer.alloc(4);tlv.writeUInt16LE(type);
    assert.throws(()=>safeMint({isInitialized:true,tlvData:tlv}));
  }
  const paused=Buffer.alloc(4+33);paused.writeUInt16LE(26);paused.writeUInt16LE(33,2);paused[36]=1;
  assert.throws(()=>safeMint({isInitialized:true,tlvData:paused}));
  paused[36]=0;safeMint({isInitialized:true,tlvData:paused});
  const hook=Buffer.alloc(4+64);hook.writeUInt16LE(14);hook.writeUInt16LE(64,2);
  safeMint({isInitialized:true,tlvData:hook});
  web3.Keypair.generate().publicKey.toBuffer().copy(hook,36);
  assert.throws(()=>safeMint({isInitialized:true,tlvData:hook}));
});

test('single unsigned v0 packet and one required signer',()=>{
  const user=web3.Keypair.generate().publicKey;
  const ix=web3.SystemProgram.transfer({fromPubkey:user,toPubkey:web3.Keypair.generate().publicKey,lamports:10});
  const tx=compile(user,web3.PublicKey.default.toBase58(),[ix],[]);
  assert.equal(tx.message.header.numRequiredSignatures,1);
  assert(tx.signatures[0].every(b=>b===0));
  assert.throws(()=>compile(web3.Keypair.generate().publicKey,web3.PublicKey.default.toBase58(),[ix],[]));
});

test('full atomic builder uses official SDK metas, lookup tables and one final slippage bound',async t=>{
  const snapshot=require('../../tests/fixtures/pump_route_accounts.json');
  const reference=require('../../tests/fixtures/pump_non_sol_buy.json');
  const a=reference.transaction.message.instructions[7].accounts;
  const b=reference.transaction.message.instructions[6].accounts;
  const values=Object.fromEntries(Object.entries(snapshot.accounts).map(([key,value])=>[key,
    {...value,owner:new web3.PublicKey(value.owner),data:Buffer.from(value.data[0],'base64')} ]));
  const curve=values[a[10]].data;
  // Synthetic reserves for an unfinished curve, never presented as mainnet state.
  curve.writeBigUInt64LE(1000000000000000n,8);curve.writeBigUInt64LE(30000000000n,16);
  curve.writeBigUInt64LE(800000000000000n,24);curve.writeBigUInt64LE(5000000000n,32);curve[48]=0;
  const user=web3.Keypair.generate().publicKey;
  const dm=require('@meteora-ag/dlmm');
  const pair={
    program:dm.createProgram(new web3.Connection('http://127.0.0.1:8899')),
    lbPair:{tokenXMint:new web3.PublicKey(b[6]),tokenYMint:new web3.PublicKey(b[7]),
      reserveX:new web3.PublicKey(b[2]),reserveY:new web3.PublicKey(b[3]),oracle:new web3.PublicKey(b[8])},
    tokenX:{owner:spl.TOKEN_2022_PROGRAM_ID,amount:100000000000n},
    tokenY:{owner:spl.TOKEN_PROGRAM_ID,amount:100000000000n},
    binArrayBitmapExtension:null,
    async getBinArrayForSwap(){return [];},
    swapQuote(amount){return {consumedInAmount:amount,outAmount:new BN(100000000),fee:new BN(0),feeOnInput:true,priceImpact:new (require('decimal.js'))(0),
      binArraysPubkey:b.slice(16).map(k=>new web3.PublicKey(k))};},
  };
  t.mock.method(dm,'create',async()=>pair);
  let simulated, simError=null;
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>values[k.toBase58()] || null);},
    async getAddressLookupTable(key){return {value:new web3.AddressLookupTableAccount({key,
      state:web3.AddressLookupTableAccount.deserialize(values[key.toBase58()].data)})};},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
    async simulateTransaction(tx){simulated=tx;return {value:{err:simError,unitsConsumed:400000}};},
  };
  const input={wallet:user.toBase58(),mint:a[1],quoteMint:a[2],tokenProgram:a[3],quoteProgram:a[4],
    pool:b[0],lookupTables:reference.transaction.message.addressTableLookups.map(t=>t.accountKey),
    minSlot:0,amount:'10000000',slippagePercent:'2',minLiquidity:'10000000000'};
  const result=await build(input,connection);
  const serialized=Buffer.from(result.transaction,'base64');
  assert(serialized.length<=1232);
  const tx=web3.VersionedTransaction.deserialize(serialized);
  const tables=await Promise.all(input.lookupTables.map(async k=>(await connection.getAddressLookupTable(new web3.PublicKey(k))).value));
  const instructions=web3.TransactionMessage.decompile(tx.message,{addressLookupTableAccounts:tables}).instructions;
  const dlmmIndex=instructions.findIndex(ix=>ix.programId.equals(pair.program.programId));
  const pumpIndex=instructions.findIndex(ix=>ix.programId.equals(pump.PUMP_PROGRAM_ID));
  assert(dlmmIndex>0 && pumpIndex>dlmmIndex);
  assert.equal(instructions[dlmmIndex].data.readBigUInt64LE(16).toString(),result.quoteIn);
  assert.equal(instructions[pumpIndex].data.readBigUInt64LE(8).toString(),result.quoteIn);
  assert.equal(instructions[pumpIndex].data.readBigUInt64LE(16).toString(),result.minOut);
  assert.equal(instructions[0].data.readUInt32LE(1),480000);
  assert(simulated.signatures[0].every(x=>x===0));
  assert.equal(new BN(result.quotedOut).muln(98).addn(99).divn(100).toString(),result.minOut);
  assert.equal(instructions.at(-1).data[0],9); // close only the newly created WSOL ATA
  curve[48]=1;
  await assert.rejects(build(input,connection),/curve-graduated/);
  curve[48]=0;simError={InstructionError:[6,{Custom:6000}]};
  await assert.rejects(build(input,connection),/simulation-rejected/);
});

test('native SOL builder spends the configured budget with common min-out and no DLMM hop',async()=>{
  const snapshot=require('../../tests/fixtures/pump_native_accounts.json');
  const reference=require('../../tests/fixtures/pump_native_multi_buy.json');
  const a=reference.transaction.message.instructions[3].accounts;
  const values=Object.fromEntries(Object.entries(snapshot.accounts).map(([k,v])=>[k,
    {...v,owner:new web3.PublicKey(v.owner),data:Buffer.from(v.data[0],'base64')} ]));
  const curve=values[a[3]].data;
  curve.writeBigUInt64LE(1000000000000000n,8);curve.writeBigUInt64LE(30000000000n,16);
  curve.writeBigUInt64LE(800000000000000n,24);curve.writeBigUInt64LE(5000000000n,32);curve[48]=0;
  const user=web3.Keypair.generate().publicKey;
  let simulationError=null,blockCalls=0;
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>values[k.toBase58()] || null);},
    async getLatestBlockhash(commitment){blockCalls++;assert.equal(commitment,'processed');return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
    async simulateTransaction(tx,options){assert.equal(options.commitment,'processed');return {value:{err:simulationError,unitsConsumed:80000}};},
  };
  const input={commitment:'processed',route:'pump_native_curve',wallet:user.toBase58(),mint:a[2],quoteMint:spl.NATIVE_MINT.toBase58(),
    tokenProgram:spl.TOKEN_PROGRAM_ID.toBase58(),lookupTables:[],minSlot:0,amount:'10000000',slippagePercent:'2'};
  const result=await build(input,connection);assert.equal(blockCalls,1);
  const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
  const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
  assert.equal(ixs.length,3); // Compute budget, base ATA, Pump buy.
  const buy=ixs[2];
  assert(buy.programId.equals(pump.PUMP_PROGRAM_ID));
  assert.equal(buy.data.subarray(0,8).toString('hex'),'38fc74089edfcd5f');
  assert.equal(buy.data.readBigUInt64LE(8),10000000n);
  assert.equal(buy.data.readBigUInt64LE(16).toString(),result.minOut);
  assert.equal(buy.data[24],1);
  assert.equal(new BN(result.quotedOut).muln(98).addn(99).divn(100).toString(),result.minOut);
  assert.equal(tx.message.header.numRequiredSignatures,1);
  assert(tx.signatures[0].every(x=>x===0));
  assert(buy.keys[6].pubkey.equals(user));
  const smaller=await build({...input,slippagePercent:'0.25'},connection);
  assert(new BN(smaller.minOut).gt(new BN(result.minOut)));
  simulationError={InstructionError:[2,{Custom:6000}]};
  await assert.rejects(build(input,connection),/simulation-rejected/);
  curve[48]=1;
  await assert.rejects(build(input,connection),/curve-graduated/);
  await assert.rejects(build({...input,quoteMint:a[2]},connection),/native-only/);
});
