'use strict';
// Emergency exit into the original quote asset, including CPMM/PumpSwap migration.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token');
const pump=require('@pump-fun/pump-sdk'),BN=require('bn.js'),crypto=require('node:crypto');
const stonk=require('./stonk.cjs');
const risk=require('./risk.cjs');
const {spotIndex:reserveIndex,SCALE}=require('./spot-index.cjs');
function sellDetails(state,amount,rate,mint,epoch){
  const transfer=spl.getTransferFeeConfig(mint);
  const net=amount-(transfer?spl.calculateEpochFee(transfer,BigInt(epoch),amount):0n);
  const x=state.virtualA-state.realA,y=state.virtualB+state.realB;
  if(net<=0n||x<=0n||y<=0n||net>state.realA)throw Error('stonk-curve-rejected');
  const gross=net*y/(x+net),fee=(gross*rate+999999n)/1000000n;
  if(gross>state.realB||gross<=fee)throw Error('stonk-curve-rejected');
  return {net:gross-fee,fees:[risk.ppm(amount-net,amount),risk.ppm(fee,gross)]};
}
function sellQuote(...args){
  return sellDetails(...args).net;
}
function sellInstruction(args){
  // Official LaunchLab sell_exact_in uses the same account metas as buy_exact_in.
  const buy=stonk.buyInstruction(args);
  crypto.createHash('sha256').update('global:sell_exact_in').digest().copy(buy.data,0,0,8);
  return buy;
}
async function buildSell(input,{connection,safeMint,finish,minimums,integer,fraction}){
  const pk=x=>new web3.PublicKey(x),user=pk(input.wallet),mint=pk(input.mint),quote=pk(input.quoteMint);
  const baseProgram=pk(input.tokenProgram),quoteProgram=pk(input.quoteProgram),amount=integer(input.amount);
  fraction(input.slippagePercent);
  for(const p of [baseProgram,quoteProgram])if(![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(x=>x.equals(p)))throw Error('token-program');
  const isStonk=['stonk_native_curve','sol_to_stonk_curve'].includes(input.route);
  const isPump=['pump_native_curve','sol_to_pump_curve','meteora_dlmm_to_pump_curve'].includes(input.route);
  if(!isStonk&&!isPump)throw Error('unknown-route');
  const native=quote.equals(spl.NATIVE_MINT);
  if(quote.equals(web3.PublicKey.default)||quote.equals(mint)||(native&&!quoteProgram.equals(spl.TOKEN_PROGRAM_ID)))throw Error('token-program');
  const pool=isStonk?pk(input.pool):pump.bondingCurvePda(mint);
  const target=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram);
  const out=spl.getAssociatedTokenAddressSync(quote,user,false,quoteProgram);
  const rows=await connection.getMultipleAccountsInfo(input.spotOnly?[mint,quote,pool]:[mint,quote,pool,target,out]);
  for(const [i,p] of [[0,baseProgram],[1,quoteProgram],[2,isStonk?stonk.PROGRAM:pump.PUMP_PROGRAM_ID]])
    if(!rows[i]||rows[i].executable||!rows[i].owner.equals(p))throw Error('account-owner');
  const base=spl.unpackMint(mint,rows[0],baseProgram),quoteInfo=spl.unpackMint(quote,rows[1],quoteProgram);
  safeMint(base,isStonk);safeMint(quoteInfo);
  // Spot monitoring validates the pool/mints but never needs a wallet balance,
  // an amount-dependent liquidation quote, fees, blockhash or simulation.
  if(input.spotOnly){
    let spotIndex,venue,spotPool=pool.toBase58();
    if(isStonk){
      const state=stonk.poolState(rows[2],pool,mint,quote,[0,2]);
      if(state.status===2){
        ({spotIndex,pool:spotPool}=await require('./cpmm-sell.cjs').prepare(input,
          {connection,state,mint,quote,baseProgram,quoteProgram,base,quoteInfo,amount}));
        venue='raydium_cpmm';
      }else{spotIndex=reserveIndex(state.virtualB+state.realB,state.virtualA-state.realA);venue='stonk_curve';}
    }else{
      const curve=pump.PUMP_SDK.decodeBondingCurve(rows[2]);
      if(!pump.normalizeQuoteMint(curve.quoteMint).equals(quote))throw Error('curve-graduated-or-quote-mismatch');
      if(curve.complete){
        ({spotIndex,pool:spotPool}=await require('./pumpswap-sell.cjs').prepare(input,
          {connection,user,mint,quoteMint:quote,baseProgram,quoteProgram,base,target,out,amount}));
        venue='pump_swap';
      }else{spotIndex=reserveIndex(curve.virtualQuoteReserves,curve.virtualTokenReserves);venue='pump_curve';}
    }
    return {mint:input.mint,quoteMint:input.quoteMint,spotIndex,indexScale:SCALE.toString(),venue,pool:spotPool};
  }
  const before=spl.unpackAccount(target,rows[3],baseProgram);
  if(!before.owner.equals(user)||!before.mint.equals(mint)||before.amount<BigInt(amount.toString())||before.isFrozen)throw Error('sell-balance-unavailable');
  const outBefore=rows[4]?spl.unpackAccount(out,rows[4],quoteProgram):null;
  if(outBefore&&(!outBefore.owner.equals(user)||!outBefore.mint.equals(quote)||outBefore.isFrozen))throw Error('account-owner');
  let expected,minOut,ixs,metrics,venue,spotIndex;
  if(isStonk){
    const state=stonk.poolState(rows[2],pool,mint,quote,[0,2]);
    let swap;
    if(state.status===2){
      const cpmm=require('./cpmm-sell.cjs');
      const route=await cpmm.prepare(input,{connection,state,mint,quote,baseProgram,quoteProgram,base,quoteInfo,amount});
      expected=new BN(route.expected.toString());metrics=route.metrics;venue='raydium_cpmm';spotIndex=route.spotIndex;
      ({minOut}=minimums(amount,expected,input.slippagePercent));
      swap=cpmm.instruction({user,mint,quote,baseProgram,quoteProgram,target,out,amount,minOut,route});
    }else{
      const configs=await connection.getMultipleAccountsInfo([state.config,stonk.PLATFORM]);
      const epoch=await connection.getEpochInfo();
      risk.checkTokenTax(input,base,epoch.epoch);
      const rate=stonk.feeSchedule(...configs,quote),details=sellDetails(state,BigInt(amount.toString()),rate,base,epoch.epoch);
      metrics=risk.checkFees(input,details.fees,[rate]);
      expected=new BN(details.net.toString());venue='stonk_curve';spotIndex=reserveIndex(state.virtualB+state.realB,state.virtualA-state.realA);
      ({minOut}=minimums(amount,expected,input.slippagePercent));
      swap=sellInstruction({user,pool,mint,quote,baseProgram,quoteProgram,state,targetAta:target,quoteAta:out,
        amount:BigInt(amount.toString()),minOut:BigInt(minOut.toString())});
    }
    ixs=[spl.createAssociatedTokenAccountIdempotentInstruction(user,out,user,quote,quoteProgram),swap];
    if(native&&!rows[4])ixs.push(spl.createCloseAccountInstruction(out,user,user));
  }else{
    venue='pump_curve';
    const curve=pump.PUMP_SDK.decodeBondingCurve(rows[2]);
    if(!pump.normalizeQuoteMint(curve.quoteMint).equals(quote))throw Error('curve-graduated-or-quote-mismatch');
    if(curve.complete){
      const swap=require('./pumpswap-sell.cjs');
      const route=await swap.prepare(input,{connection,user,mint,quoteMint:quote,baseProgram,quoteProgram,base,
        target,out,sourceRow:rows[3],quoteRow:rows[4],amount});
      expected=route.expected;metrics=route.metrics;venue='pump_swap';spotIndex=route.spotIndex;
      ({minOut}=minimums(amount,expected,input.slippagePercent));
      ixs=await swap.instructions(route,amount,minOut);
    }else{
      const configs=await connection.getMultipleAccountsInfo([pump.GLOBAL_PDA,pump.PUMP_FEE_CONFIG_PDA]);
      [pump.PUMP_PROGRAM_ID,pump.PUMP_FEE_PROGRAM_ID].forEach((p,i)=>{
        if(!configs[i]||configs[i].executable||!configs[i].owner.equals(p))throw Error('account-owner');
      });
      spotIndex=reserveIndex(curve.virtualQuoteReserves,curve.virtualTokenReserves);
      const global=pump.PUMP_SDK.decodeGlobal(configs[0]),feeConfig=pump.PUMP_SDK.decodeFeeConfig(configs[1]);
      expected=pump.getSellSolAmountFromTokenAmount({global,feeConfig,mintSupply:new BN(base.supply.toString()),bondingCurve:curve,amount});
      metrics=require('./pump-risk.cjs').checkFees(input,{global,feeConfig,mintSupply:new BN(base.supply.toString()),
        curve,amount,expected,sell:true});
      ({minOut}=minimums(amount,expected,input.slippagePercent));
      const args={global,bondingCurveAccountInfo:rows[2],bondingCurve:curve,mint,user,amount,slippage:0,tokenProgram:baseProgram};
      if(native)ixs=await pump.PUMP_SDK.sellInstructions({...args,solAmount:minOut,
        mayhemMode:curve.isMayhemMode,cashback:curve.isCashbackCoin});
      else ixs=[spl.createAssociatedTokenAccountIdempotentInstruction(user,out,user,quote,quoteProgram),
        ...await pump.PUMP_SDK.sellV2Instructions({...args,quoteAmount:minOut,quoteTokenProgram:quoteProgram})];
    }
  }
  // Monitoring needs a validated executable quote, not a blockhash or simulation.
  if(input.quoteOnly)return {side:'sell',wallet:user.toBase58(),mint:mint.toBase58(),
    amount:amount.toString(),quoteMint:quote.toBase58(),quotedOut:expected.toString(),
    minOut:minOut.toString(),risk:metrics,venue,spotIndex,indexScale:SCALE.toString()};
  const checkQuote=!native||(isStonk||venue==='pump_swap')&&!!rows[4];
  const check={addresses:[target.toBase58(),...(checkQuote?[out.toBase58()]:[])],verify(values){
    const decode=(address,row,program)=>{
      if(!row)throw Error('sell-fill-rejected');
      return spl.unpackAccount(address,{...row,owner:pk(row.owner),data:Buffer.from(row.data[0],'base64')},program);
    };
    const post=decode(target,values?.[0],baseProgram);
    if(!post.owner.equals(user)||!post.mint.equals(mint)||before.amount-post.amount!==BigInt(amount.toString()))throw Error('sell-fill-rejected');
    if(checkQuote){
      const received=decode(out,values?.[1],quoteProgram);
      if(!received.owner.equals(user)||!received.mint.equals(quote)||received.amount-(outBefore?.amount||0n)<BigInt(minOut.toString()))throw Error('sell-fill-rejected');
    }
  }};
  return {...await finish(connection,input,user,ixs,check),side:'sell',wallet:user.toBase58(),mint:mint.toBase58(),
    amount:amount.toString(),quoteMint:quote.toBase58(),quotedOut:expected.toString(),minOut:minOut.toString(),risk:metrics,venue,spotIndex,indexScale:SCALE.toString()};
}
module.exports={buildSell,sellInstruction,sellQuote};
