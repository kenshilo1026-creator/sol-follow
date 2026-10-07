'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const crypto=require('node:crypto');
const {build}=require('../../trade_execution/sdk/build.cjs');
const {sellQuote}=require('../../trade_execution/sdk/sell.cjs');
const pk=x=>new web3.PublicKey(x);
for(const kind of ['pump_native_curve','sol_to_pump_curve','stonk_native_curve','sol_to_stonk_curve']){
  test(`emergency ${kind} sells full input with shared slippage and no quote API`,async t=>{
    t.mock.method(global,'fetch',async()=>assert.fail('no quote API or external RPC in offline test'));
    const stonk=kind.includes('stonk'),native=kind.includes('native');
    const fixture=require('../fixtures/'+(stonk?(native?'stonk_native_accounts.json':'stonk_route_accounts.json'):
      (native?'pump_native_accounts.json':'pump_route_accounts.json')));
    const ref=require('../fixtures/'+(stonk?(native?'stonk_native_buy.json':'stonk_non_sol_buy.json'):
      (native?'pump_native_multi_buy.json':'pump_non_sol_buy.json')));
    const a=ref.transaction.message.instructions[stonk?(native?6:4):(native?3:7)].accounts;
    const mint=pk(a[stonk?9:(native?2:1)]),quote=native?spl.NATIVE_MINT:pk(a[stonk?10:2]);
    const pool=pk(a[stonk?4:(native?3:10)]);
    const rows=Object.fromEntries(Object.entries(fixture.accounts).filter(([,r])=>r).map(([k,r])=>
      [k,{...r,owner:pk(r.owner),data:Buffer.from(r.data[0],'base64')}]));
    const curve=rows[pool.toBase58()].data;
    if(stonk){
      curve[17]=0;
      for(const [offset,v] of [[29,793100000000000n],[37,1073000000000000n],[45,30000000000n],
        [53,138432646230590n],[61,4443750000n]])curve.writeBigUInt64LE(v,offset);
    }else{
      for(const [offset,v] of [[8,1000000000000000n],[16,30000000000n],[24,800000000000000n],[32,5000000000n]])curve.writeBigUInt64LE(v,offset);
      curve[48]=0;
    }
    const user=web3.Keypair.generate().publicKey,program=rows[mint.toBase58()].owner;
    const quoteProgram=native?spl.TOKEN_PROGRAM_ID:rows[quote.toBase58()].owner;
    if(!rows[quote.toBase58()]){
      const data=Buffer.alloc(82);data[44]=9;data[45]=1;
      rows[quote.toBase58()]={data,owner:quoteProgram,executable:false,lamports:1};
    }
    const target=spl.getAssociatedTokenAddressSync(mint,user,false,program),out=spl.getAssociatedTokenAddressSync(quote,user,false,quoteProgram);
    function token(mint,amount,owner){
      const data=Buffer.alloc(165);mint.toBuffer().copy(data);user.toBuffer().copy(data,32);
      data.writeBigUInt64LE(amount,64);data[108]=1;
      return {data,owner,executable:false,lamports:2000000};
    }
    rows[target.toBase58()]=token(mint,2000000000n,program);
    rows[out.toBase58()]=token(quote,50n,quoteProgram); // Preserve existing quote/WSOL.
    let badFill=false,simulationError=null;
    const connection={
      async getMultipleAccountsInfo(keys){return keys.map(k=>rows[k.toBase58()]||null);},
      async getEpochInfo(){return {epoch:2000};},
      async getLatestBlockhash(){return {blockhash:web3.PublicKey.default.toBase58(),lastValidBlockHeight:100};},
      async simulateTransaction(tx,options){
        const values=options.accounts.addresses.map(k=>k===target.toBase58()?
          token(mint,badFill?0n:1000000000n,program):token(quote,1000000000000n,quoteProgram));
        return {value:{err:simulationError,unitsConsumed:150000,accounts:values.map(r=>
          ({...r,owner:r.owner.toBase58(),data:[r.data.toString('base64'),'base64']}))}};
      },
    };
    const input={side:'sell',route:kind,wallet:user.toBase58(),mint:mint.toBase58(),quoteMint:quote.toBase58(),
      tokenProgram:program.toBase58(),quoteProgram:quoteProgram.toBase58(),pool:pool.toBase58(),lookupTables:[],
      minSlot:0,amount:'1000000000',slippagePercent:'2',commitment:'confirmed'};
    const result=await build(input,connection);
    assert.equal(result.side,'sell');assert.equal(result.amount,input.amount);
    assert.equal(BigInt(result.minOut),(BigInt(result.quotedOut)*98n+99n)/100n);
    const tx=web3.VersionedTransaction.deserialize(Buffer.from(result.transaction,'base64'));
    assert(tx.signatures[0].every(x=>x===0));assert.equal(tx.message.header.numRequiredSignatures,1);
    const ixs=web3.TransactionMessage.decompile(tx.message).instructions;
    const sell=ixs.find(ix=>ix.programId.equals(stonk?pk('LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj'):pump.PUMP_PROGRAM_ID));
    const name=stonk?'sell_exact_in':native?'sell':'sell_v2';
    assert(sell.data.subarray(0,8).equals(crypto.createHash('sha256').update('global:'+name).digest().subarray(0,8)));
    assert.equal(sell.data.readBigUInt64LE(8),1000000000n);assert.equal(sell.data.readBigUInt64LE(16),BigInt(result.minOut));
    assert(!ixs.some(ix=>ix.programId.equals(web3.SystemProgram.programId))); // No new SOL budget.
    badFill=true;await assert.rejects(build(input,connection),/sell-fill-rejected/);badFill=false;
    simulationError={InstructionError:[0,'failed']};await assert.rejects(build(input,connection),/simulation-rejected/);simulationError=null;
    await assert.rejects(build({...input,amount:'2000000001'},connection),/sell-balance-unavailable/);
    curve[stonk?17:48]=stonk?2:1;
    await assert.rejects(build(input,connection),/stonk-pool-rejected|curve-graduated/);
  });
}
test('Stonk exact-in sell rounds fees upward and deducts base transfer tax',()=>{
  const state={virtualA:1000000n,realA:100000n,virtualB:1000000n,realB:1000000n};
  const base={tlvData:Buffer.alloc(0)};
  const gross=1000n*2000000n/901000n;
  assert.equal(sellQuote(state,1000n,12500n,base,2000),gross-(gross*12500n+999999n)/1000000n);
  assert.throws(()=>sellQuote(state,100001n,12500n,base,2000));
});
