'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token');
const {build}=require('../../trade_execution/sdk/build.cjs');
const {buildReverse}=require('../../trade_execution/sdk/local-routes.cjs');
const pk=x=>new web3.PublicKey(x);
test('WSOL proceeds unwrap through a temporary account without closing the original ATA',async()=>{
  const user=web3.Keypair.generate().publicKey,source=spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user);
  const mint=Buffer.alloc(82);mint[44]=9;mint[45]=1;
  function row(amount){
    const data=Buffer.alloc(165);spl.NATIVE_MINT.toBuffer().copy(data);user.toBuffer().copy(data,32);
    data.writeBigUInt64LE(amount,64);data[108]=1;data.writeUInt32LE(1,109);data.writeBigUInt64LE(2039280n,113);
    return {owner:spl.TOKEN_PROGRAM_ID,data,executable:false,lamports:2039280+Number(amount)};
  }
  const connection={
    async getMultipleAccountsInfo(keys){return keys.map(k=>k.equals(source)?row(2000n):k.equals(spl.NATIVE_MINT)?
      {owner:spl.TOKEN_PROGRAM_ID,data:mint,executable:false,lamports:1}:null);},
    async getMinimumBalanceForRentExemption(){return 2039280;},
    async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
    async simulateTransaction(){const r=row(1000n);return {value:{err:null,unitsConsumed:50000,accounts:[
      {...r,owner:r.owner.toBase58(),data:[r.data.toString('base64'),'base64']} ]}};},
  };
  const result=await build({side:'sweep',route:'quote_to_sol',wallet:user.toBase58(),mint:spl.NATIVE_MINT.toBase58(),
    tokenProgram:spl.TOKEN_PROGRAM_ID.toBase58(),amount:'1000',minSlot:0,lookupTables:[],slippagePercent:'20',
    risk:{poolFeeBps:200,totalFeeBps:300,impactBps:200}},connection);
  assert.equal(result.minOut,'1000');assert.equal(result.risk.feePpm,'0');
  const ixs=web3.TransactionMessage.decompile(web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64')).message).instructions;
  assert.equal(ixs.at(-1).data[0],9);assert(!ixs.at(-1).keys[0].pubkey.equals(source));
  const move=ixs.find(ix=>ix.programId.equals(spl.TOKEN_PROGRAM_ID)&&ix.data[0]===3);
  assert(move.keys[0].pubkey.equals(source));assert.equal(move.data.readBigUInt64LE(1),1000n);
});
test('reverse path and per-hop floors use quote -> SOL, never a new SOL spend',async()=>{
  const mid=web3.Keypair.generate().publicKey.toBase58(),mint=web3.Keypair.generate().publicKey;
  const recipe={version:1,tables:[],steps:[
    {label:'Whirlpool',pool:web3.Keypair.generate().publicKey.toBase58(),inputMint:spl.NATIVE_MINT.toBase58(),outputMint:mid},
    {label:'Whirlpool',pool:web3.Keypair.generate().publicKey.toBase58(),inputMint:mid,outputMint:mint.toBase58()}]};
  const seen=[];
  const r=await buildReverse({quoteMint:mint,amount:10000n,slippageBps:100},recipe,async step=>{
    seen.push(step);return {setup:[],quote:async amount=>({out:amount*2n,fee:1000n,impact:0n,instructions:()=>[]})};
  });
  assert.deepEqual(seen.map(s=>s.inputMint),[mint.toBase58(),mid]);
  assert.equal(seen.at(-1).outputMint,spl.NATIVE_MINT.toBase58());
  assert(r.minimum>=r.expected*99n/100n);
});

for(const name of ['local_quote_accounts.json','local_dlmm_quote_accounts.json']){
  test(`background conversion builds reversed ${name}, caps fees and tightens 20% slippage`,async t=>{
    const fixture=require('../fixtures/'+name);t.mock.method(Date,'now',()=>fixture.timestamp);
    t.mock.method(global,'fetch',async()=>assert.fail('no external request in offline test'));
    const user=web3.Keypair.generate().publicKey,mint=pk(fixture.recipe.steps.at(-1).outputMint);
    const rows=Object.fromEntries(Object.entries(fixture.accounts).filter(([,r])=>r).map(([k,r])=>
      [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));
    const program=rows[mint.toBase58()].owner,source=spl.getAssociatedTokenAddressSync(mint,user,false,program);
    function account(amount){
      const data=Buffer.alloc(165);mint.toBuffer().copy(data);user.toBuffer().copy(data,32);
      data.writeBigUInt64LE(amount,64);data[108]=1;
      return {owner:program,data,executable:false,lamports:2000000};
    }
    rows[source.toBase58()]=account(200000000n);
    let steal=false;
    const connection={
      async getAccountInfo(k){return rows[k.toBase58()]||null;},
      async getMultipleAccountsInfo(keys){return keys.map(k=>rows[k.toBase58()]||null);},
      async getMultipleAccountsInfoAndContext(keys){return {context:{slot:999999999},value:await this.getMultipleAccountsInfo(keys)};},
      async getEpochInfo(){return fixture.epoch||require('../fixtures/local_quote_accounts.json').epoch;},
      async getAddressLookupTable(k){return {value:new web3.AddressLookupTableAccount({key:k,state:web3.AddressLookupTableAccount.deserialize(rows[k.toBase58()].data)})};},
      async getMinimumBalanceForRentExemption(){return 2039280;},
      async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:900};},
      async simulateTransaction(tx,options){
        const row=account(steal?0n:100000000n);
        return {value:{err:null,unitsConsumed:500000,accounts:options.accounts.addresses.map(k=>{
          assert.equal(k,source.toBase58());return {...row,owner:row.owner.toBase58(),data:[row.data.toString('base64'),'base64']};
        })}};
      },
    };
    const input={side:'sweep',route:'quote_to_sol',wallet:user.toBase58(),mint:mint.toBase58(),tokenProgram:program.toBase58(),
      amount:'100000000',minSlot:0,lookupTables:[],slippagePercent:'20',minLiquidity:'0',swapRecipe:fixture.recipe,
      risk:{poolFeeBps:200,totalFeeBps:300,impactBps:200}};
    const result=await build(input,connection);
    assert.equal(result.side,'sweep');assert.equal(result.amount,input.amount);
    assert(BigInt(result.minOut)>=BigInt(result.quotedOut)*98n/100n-2n);
    const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
    assert(tx.signatures[0].every(x=>x===0));assert.equal(tx.message.header.numRequiredSignatures,1);
    const tables=await Promise.all(fixture.recipe.tables.map(async k=>(await connection.getAddressLookupTable(pk(k))).value));
    const ixs=web3.TransactionMessage.decompile(tx.message,{addressLookupTableAccounts:tables}).instructions;
    const close=ixs.at(-1);assert(close.programId.equals(spl.TOKEN_PROGRAM_ID)&&close.data[0]===9);
    assert(!close.keys[0].pubkey.equals(spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user)));
    assert(close.keys[1].pubkey.equals(user));
    await assert.rejects(build({...input,risk:{...input.risk,totalFeeBps:0}},connection),/total-fee-limit/);
    await assert.rejects(build({...input,risk:{...input.risk,poolFeeBps:0}},connection),/pool-fee-limit/);
    steal=true;await assert.rejects(build(input,connection),/sell-fill-rejected/);
  });
}
