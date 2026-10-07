'use strict';
// Persist identities, never executable quotes. All amounts come from current
// account snapshots through the same slot/freshness barrier as the final curve.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const Decimal=require('decimal.js');
const orca=require('@orca-so/whirlpools-sdk');
const {Percentage}=require('@orca-so/common-sdk');
const dm=require('@meteora-ag/dlmm'),DLMM=dm.default||dm;
const risk=require('./risk.cjs');
const DLMM_ID=new web3.PublicKey('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo');
const MEMO=new web3.PublicKey('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr');
const pk=x=>new web3.PublicKey(x),big=x=>BigInt(x.toString()),bn=x=>new BN(x.toString());
function validateRecipe(recipe,quoteMint){
  if(recipe?.version!==1||!Array.isArray(recipe.steps)||recipe.steps.length<1||recipe.steps.length>3
    ||!Array.isArray(recipe.tables)||recipe.tables.length>8)throw Error('cached-route-rejected');
  let last=spl.NATIVE_MINT.toBase58();const seen=new Set([last]),pools=new Set();
  for(const s of recipe.steps){
    if(!['Whirlpool','Meteora DLMM'].includes(s.label)||s.inputMint!==last||seen.has(s.outputMint)||pools.has(s.pool))throw Error('cached-route-rejected');
    pk(s.pool);pk(s.inputMint);pk(s.outputMint);last=s.outputMint;seen.add(last);pools.add(s.pool);
  }
  if(last!==quoteMint.toBase58())throw Error('cached-route-rejected');
  recipe.tables.forEach(pk);return recipe;
}
function recipeFrom(body,quoteMint){
  const steps=(body.routePlan||[]).map(r=>{
    if((r.bps??10000)!==10000||(r.percent??100)!==100)throw Error('cached-route-rejected');
    const s=r.swapInfo;return {label:s.label,pool:s.ammKey,inputMint:s.inputMint,outputMint:s.outputMint};
  });
  return validateRecipe({version:1,steps,tables:Object.keys(body.addressesByLookupTableAddress||{})},quoteMint);
}
function dlmmRisk(q){
  if(!q.fee||!q.priceImpact||typeof q.feeOnInput!=='boolean')throw Error('invalid-risk-data');
  const fee=big(q.fee),out=big(q.outAmount),input=big(q.consumedInAmount);
  const impact=new Decimal(q.priceImpact.toString()).abs().mul(10000).ceil();
  if(!impact.isFinite()||impact.gt(1000000))throw Error('invalid-risk-data');
  return {fee:risk.ppm(fee,q.feeOnInput?input:out+fee),impact:BigInt(impact.toFixed(0))};
}
async function mints(connection,keys,safeMint){
  const rows=await connection.getMultipleAccountsInfo(keys);
  return rows.map((row,i)=>{
    if(!row||row.executable||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(row.owner)))throw Error('token-program');
    safeMint(spl.unpackMint(keys[i],row,row.owner));return row.owner;
  });
}
async function adapter(step,args){
  const {connection,user,safeMint,minLiquidity}=args,pool=pk(step.pool),inputMint=pk(step.inputMint);
  const expectedProgram=step.label==='Whirlpool'?orca.ORCA_WHIRLPOOL_PROGRAM_ID:DLMM_ID;
  const row=await connection.getAccountInfo(pool);
  if(!row||row.executable||!row.owner.equals(expectedProgram))throw Error('account-owner');
  if(step.label==='Whirlpool'){
    const ctx=orca.WhirlpoolContext.from(connection,{publicKey:user,
      signTransaction:async()=>{throw Error('unexpected-signer');},signAllTransactions:async()=>{throw Error('unexpected-signer');}});
    const data=await ctx.fetcher.getPool(pool,orca.IGNORE_CACHE);
    if(!data||![data.tokenMintA,data.tokenMintB].some(m=>m.equals(inputMint))
      ||![data.tokenMintA,data.tokenMintB].some(m=>m.toBase58()===step.outputMint))throw Error('cached-route-rejected');
    const programs=await mints(connection,[data.tokenMintA,data.tokenMintB],safeMint);
    const atas=[data.tokenMintA,data.tokenMintB].map((m,i)=>args.nativeAccount&&m.equals(spl.NATIVE_MINT)?args.nativeAccount:spl.getAssociatedTokenAddressSync(m,user,false,programs[i]));
    const setup=[data.tokenMintA,data.tokenMintB].flatMap((m,i)=>args.nativeAccount&&m.equals(spl.NATIVE_MINT)?[]:[spl.createAssociatedTokenAccountIdempotentInstruction(user,atas[i],user,m,programs[i])]);
    if([inputMint.toBase58(),step.outputMint].includes(spl.NATIVE_MINT.toBase58())){
      const vault=data.tokenMintA.equals(spl.NATIVE_MINT)?data.tokenVaultA:data.tokenVaultB;
      const v=await connection.getAccountInfo(vault);
      if(!v||spl.unpackAccount(vault,v,spl.TOKEN_PROGRAM_ID).amount<BigInt(minLiquidity))throw Error('insufficient-sol-liquidity');
    }
    const whirlpool={getAddress:()=>pool,getData:()=>data};
    return {setup,async quote(amount){
      const q=await orca.swapQuoteByInputToken(whirlpool,inputMint,bn(amount),Percentage.fromFraction(0,100),
        expectedProgram,ctx.fetcher,orca.IGNORE_CACHE);
      if(big(q.estimatedAmountIn)!==amount)throw Error('partial-swap');
      const fee=big(q.estimatedFeeAmount),effective=amount-fee,square=big(data.sqrtPrice)**2n,scale=1n<<128n;
      const ideal=q.aToB?effective*square/scale:effective*scale/square;
      const out=big(q.estimatedAmountOut);
      return {out,fee:risk.ppm(fee,amount),impact:risk.impact(out,ideal),
        instructions(minimum){
          return orca.WhirlpoolIx.swapV2Ix(ctx.program,{...q,amount:bn(amount),otherAmountThreshold:bn(minimum),
            whirlpool:pool,tokenAuthority:user,tokenMintA:data.tokenMintA,tokenMintB:data.tokenMintB,
            tokenOwnerAccountA:atas[0],tokenOwnerAccountB:atas[1],tokenVaultA:data.tokenVaultA,tokenVaultB:data.tokenVaultB,
            tokenProgramA:programs[0],tokenProgramB:programs[1],oracle:orca.PDAUtil.getOracle(expectedProgram,pool).publicKey}).instructions;
        }};
    }};
  }
  const pair=await DLMM.create(connection,pool),x=pair.lbPair.tokenXMint,y=pair.lbPair.tokenYMint;
  const swapForY=x.equals(inputMint);
  if(!(swapForY?x:y).equals(inputMint)||(swapForY?y:x).toBase58()!==step.outputMint)throw Error('cached-route-rejected');
  const programs=await mints(connection,[x,y],safeMint);
  if(!programs[0].equals(pair.tokenX.owner)||!programs[1].equals(pair.tokenY.owner))throw Error('dlmm-token-program');
  if([inputMint.toBase58(),step.outputMint].includes(spl.NATIVE_MINT.toBase58())&&(x.equals(spl.NATIVE_MINT)?pair.tokenX:pair.tokenY).amount<BigInt(minLiquidity))throw Error('insufficient-sol-liquidity');
  const atas=[x,y].map((m,i)=>args.nativeAccount&&m.equals(spl.NATIVE_MINT)?args.nativeAccount:spl.getAssociatedTokenAddressSync(m,user,false,programs[i]));
  const setup=[x,y].flatMap((m,i)=>args.nativeAccount&&m.equals(spl.NATIVE_MINT)?[]:[spl.createAssociatedTokenAccountIdempotentInstruction(user,atas[i],user,m,programs[i])]);
  const bins=await pair.getBinArrayForSwap(swapForY);
  return {setup,async quote(amount){
    const q=pair.swapQuote(bn(amount),swapForY,new BN(0),bins,false);
    if(big(q.consumedInAmount)!==amount)throw Error('partial-swap');
    return {out:big(q.outAmount),...dlmmRisk(q),async instructions(minimum){
      return [await pair.program.methods.swap2(bn(amount),bn(minimum),{slices:[]}).accountsPartial({
        lbPair:pool,reserveX:pair.lbPair.reserveX,reserveY:pair.lbPair.reserveY,tokenXMint:x,tokenYMint:y,
        tokenXProgram:programs[0],tokenYProgram:programs[1],user,userTokenIn:atas[swapForY?0:1],userTokenOut:atas[swapForY?1:0],
        binArrayBitmapExtension:pair.binArrayBitmapExtension?.publicKey||null,oracle:pair.lbPair.oracle,hostFeeIn:null,memoProgram:MEMO,
      }).remainingAccounts(q.binArraysPubkey.map(pubkey=>({pubkey,isSigner:false,isWritable:true}))).instruction()];
    }};
  }};
}
async function buildLocal(args,recipe,makeAdapter=adapter){
  validateRecipe(recipe,args.quoteMint);
  return buildSteps(args,recipe,makeAdapter);
}
async function buildSteps(args,recipe,makeAdapter=adapter){
  let expected=big(args.amount),minimum=expected;
  const setup=[],swaps=[],fees=[],impacts=[];
  const bps=Math.floor(args.slippageBps/recipe.steps.length);
  for(const step of recipe.steps){
    const a=await makeAdapter(step,args),full=await a.quote(expected);
    const guarded=minimum===expected?full:await a.quote(minimum);
    if(full.out<=0n||guarded.out<=0n)throw Error('dust-route');
    expected=full.out;minimum=guarded.out*BigInt(10000-bps)/10000n;
    if(minimum<=0n)throw Error('dust-route');
    setup.push(...a.setup);swaps.push(...await guarded.instructions(minimum));
    fees.push(full.fee>guarded.fee?full.fee:guarded.fee);impacts.push(full.impact>guarded.impact?full.impact:guarded.impact);
  }
  return {setup,swaps,tables:recipe.tables,expected,minimum,fees,impacts,swapRecipe:recipe};
}
async function buildReverse(args,recipe,makeAdapter=adapter){
  validateRecipe(recipe,args.quoteMint);
  const reversed={...recipe,steps:[...recipe.steps].reverse().map(s=>({...s,inputMint:s.outputMint,outputMint:s.inputMint}))};
  return buildSteps(args,reversed,makeAdapter);
}
module.exports={buildLocal,buildReverse,recipeFrom,validateRecipe,dlmmRisk};
