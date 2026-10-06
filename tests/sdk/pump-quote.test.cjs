'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const {buildPumpQuote}=require('../../trade_execution/sdk/pump-quote.cjs');
const {build,safeMint,minimums,exactQuoteInstruction}=require('../../trade_execution/sdk/build.cjs');
const BN=require('bn.js');
const ref=require('../fixtures/pump_non_sol_buy.json'),pumpRows=require('../fixtures/pump_route_accounts.json');
const pk=k=>new web3.PublicKey(k);
for(const [fixtureName,steps] of [['local_quote_accounts.json',1],['local_quote_accounts.json',2],['local_dlmm_quote_accounts.json',1]]){
  test(`generic Pump quote uses real cached ${fixtureName} (${steps} hops) without a mint whitelist or quote API`,async t=>{
    const fixture=structuredClone(require('../fixtures/'+fixtureName));fixture.recipe.steps=fixture.recipe.steps.slice(0,steps);t.mock.method(Date,'now',()=>fixture.timestamp);
    const a=ref.transaction.message.instructions[7].accounts,user=web3.Keypair.generate().publicKey;
    const quote=fixture.recipe.steps.at(-1).outputMint;
    const values=Object.fromEntries(Object.entries({...pumpRows.accounts,...fixture.accounts}).filter(([,v])=>v).map(([k,v])=>
      [k,{...v,owner:pk(v.owner),data:Buffer.from(v.data[0],'base64')}]));
    // Synthetic ungraduated Pump curve paired with DJT, fed by real route snapshots.
    const curve=values[a[10]].data,offset=curve.indexOf(pk(a[2]).toBuffer());assert(offset>=0);
    pk(quote).toBuffer().copy(curve,offset);
    curve.writeBigUInt64LE(1000000000000000n,8);curve.writeBigUInt64LE(30000000000n,16);
    curve.writeBigUInt64LE(800000000000000n,24);curve.writeBigUInt64LE(5000000000n,32);curve[48]=0;
    const input={route:'sol_to_pump_curve',wallet:user.toBase58(),mint:a[1],quoteMint:quote,tokenProgram:a[3],
      quoteProgram:values[quote].owner.toBase58(),pool:a[10],lookupTables:[],minSlot:0,amount:'10000000',slippagePercent:'2',
      minLiquidity:'0',swapRecipe:fixture.recipe};
    const connection={
      async getAccountInfo(k){return values[k.toBase58()]||null;},
      async getMultipleAccountsInfo(keys){return keys.map(k=>values[k.toBase58()]||null);},
      async getMultipleAccountsInfoAndContext(keys){return {context:{slot:999999999},value:await this.getMultipleAccountsInfo(keys)};},
      async getEpochInfo(){return fixture.epoch||require('../fixtures/local_quote_accounts.json').epoch;},
    };
    const tokenRow=(mint,owner,amount)=>{const data=Buffer.alloc(165);pk(mint).toBuffer().copy(data);owner.toBuffer().copy(data,32);
      data.writeBigUInt64LE(amount,64);data[108]=1;return {data:[data.toString('base64'),'base64'],owner:a[3],executable:false,lamports:2000000};};
    let ixs,verification;
    const helpers={connection,safeMint,minimums,exactQuoteInstruction,integer:x=>new BN(x),
      fraction:()=>({numerator:new BN(2),denominator:new BN(100)}),async finish(c,request,owner,instructions,check){
        ixs=instructions;verification=check;
        assert.deepEqual(request.lookupTables,fixture.recipe.tables);
        check.verify([tokenRow(a[1],user,1000000000000000n)]);
        assert.throws(()=>check.verify([tokenRow(a[1],user,0n)]),/pump-fill-rejected/);
        return {transaction:'fixture-unsigned'};
      }};
    t.mock.method(global,'fetch',async()=>{throw Error('network-forbidden');});
    const result=await buildPumpQuote(input,helpers);
    const swaps=ixs.filter(ix=>fixture.recipe.steps.some(s=>ix.programId.toBase58()===(s.label==='Whirlpool'
      ?'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc':'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo')));
    assert.equal(swaps.length,fixture.recipe.steps.length);
    const buy=ixs.find(ix=>ix.programId.equals(pump.PUMP_PROGRAM_ID));
    assert.equal(buy.data.readBigUInt64LE(8).toString(),result.quoteIn);
    assert.equal(buy.data.readBigUInt64LE(16).toString(),result.minOut);
    assert.equal(BigInt(result.minOut),(BigInt(result.quotedOut)*98n+99n)/100n);
    assert.equal(buy.keys[2].pubkey.toBase58(),quote);
    assert(BigInt(result.quoteIn)<=BigInt(result.quoteOut));
    assert.equal(verification.addresses.length,1);
    await assert.rejects(buildPumpQuote({...input,risk:{poolFeeBps:0,totalFeeBps:0,impactBps:200}},helpers),/pool-fee-limit/);
    curve[48]=1;await assert.rejects(buildPumpQuote(input,helpers),/curve-graduated/);
    curve[48]=0;values[fixture.recipe.steps[0].pool].owner=web3.SystemProgram.programId;
    await assert.rejects(buildPumpQuote(input,helpers),/account-owner/);
  });
}
