'use strict';
// Reverse a previously validated route; fresh pool reads, no discovery API.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),crypto=require('node:crypto');
const {buildReverse,validateRecipe}=require('./local-routes.cjs'),risk=require('./risk.cjs');
async function buildSweep(input,{connection,safeMint,finish,integer,fraction}){
  const pk=x=>new web3.PublicKey(x),user=pk(input.wallet),mint=pk(input.mint),program=pk(input.tokenProgram);
  const amount=integer(input.amount),rawAmount=BigInt(amount.toString());
  if(![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(program)))throw Error('token-program');
  const source=spl.getAssociatedTokenAddressSync(mint,user,false,program);
  const rows=await connection.getMultipleAccountsInfo([mint,source]);
  if(!rows[0]||!rows[0].owner.equals(program)||rows[0].executable)throw Error('account-owner');
  safeMint(spl.unpackMint(mint,rows[0],program));
  const before=spl.unpackAccount(source,rows[1],program);
  if(!before.owner.equals(user)||!before.mint.equals(mint)||before.amount<rawAmount||before.isFrozen)throw Error('sell-balance-unavailable');
  const seed=crypto.randomBytes(16).toString('hex');
  const native=await web3.PublicKey.createWithSeed(user,seed,spl.TOKEN_PROGRAM_ID);
  const rent=await connection.getMinimumBalanceForRentExemption(spl.ACCOUNT_SIZE);
  const setup=[web3.SystemProgram.createAccountWithSeed({fromPubkey:user,basePubkey:user,seed,newAccountPubkey:native,
    lamports:rent,space:spl.ACCOUNT_SIZE,programId:spl.TOKEN_PROGRAM_ID}),
    spl.createInitializeAccount3Instruction(native,spl.NATIVE_MINT,user)];
  let hop;
  if(mint.equals(spl.NATIVE_MINT)){
    if(!program.equals(spl.TOKEN_PROGRAM_ID))throw Error('token-program');
    hop={setup:[],swaps:[spl.createTransferInstruction(source,native,user,rawAmount)],tables:[],
      expected:rawAmount,minimum:rawAmount,fees:[],impacts:[]};
  }else{
    validateRecipe(input.swapRecipe,mint);
    const {numerator,denominator}=fraction(input.slippagePercent);
    // Never apply a 20% buy slippage allowance to a patient background conversion.
    const slippageBps=Math.min(numerator.muln(10000).div(denominator).toNumber(),
      input.risk.totalFeeBps,input.risk.impactBps);
    hop=await buildReverse({connection,user,quoteMint:mint,amount,safeMint,nativeAccount:native,
      minLiquidity:input.minLiquidity,slippageBps},input.swapRecipe);
  }
  const metrics=risk.check(input,hop.fees,hop.impacts);
  const quoteTime=Date.now();
  // Preserve all pre-existing balances touched by the route, including intermediate mints.
  const touched=[...new Set([source.toBase58(),...hop.swaps.flatMap(ix=>ix.keys.filter(k=>k.isWritable).map(k=>k.pubkey.toBase58()))])];
  if(touched.length>96)throw Error('cached-route-rejected');
  const prior=await connection.getMultipleAccountsInfo(touched.map(pk)),protectedAccounts=[];
  prior.forEach((row,i)=>{
    if(!row||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(row.owner)))return;
    const address=pk(touched[i]),account=spl.unpackAccount(address,row,row.owner);
    if(account.owner.equals(user))protectedAccounts.push({address,program:row.owner,account});
  });
  const check={addresses:protectedAccounts.map(x=>x.address.toBase58()),verify(values){
    for(let i=0;i<protectedAccounts.length;i++){
      const old=protectedAccounts[i],row=values?.[i];
      if(!row)throw Error('sell-fill-rejected');
      const after=spl.unpackAccount(old.address,{...row,owner:pk(row.owner),data:Buffer.from(row.data[0],'base64')},old.program);
      const expected=old.account.amount-(old.address.equals(source)?rawAmount:0n);
      if(!after.owner.equals(user)||!after.mint.equals(old.account.mint)||after.amount<expected
        ||old.address.equals(source)&&after.amount!==expected
        ||after.delegate?.toBase58()!==old.account.delegate?.toBase58()
        ||after.delegatedAmount!==old.account.delegatedAmount
        ||after.closeAuthority?.toBase58()!==old.account.closeAuthority?.toBase58())throw Error('sell-fill-rejected');
    }
  }};
  const result=await finish(connection,{...input,lookupTables:hop.tables},user,
    [...setup,...hop.setup,...hop.swaps,spl.createCloseAccountInstruction(native,user,user)],check);
  if(Date.now()-quoteTime>5000)throw Error('sweep-quote-expired');
  return {...result,side:'sweep',wallet:user.toBase58(),mint:mint.toBase58(),amount:amount.toString(),
    quotedOut:hop.expected.toString(),minOut:hop.minimum.toString(),quoteTime,risk:metrics};
}
module.exports={buildSweep};
