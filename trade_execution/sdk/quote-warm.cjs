"use strict";
// Warm SOL->quote independently of any previous target mint or graduation state.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token');
async function warm(input,connection){
  const {safeMint,integer,fraction}=require('./build.cjs');
  const user=new web3.PublicKey(input.wallet),quote=new web3.PublicKey(input.quoteMint),program=new web3.PublicKey(input.quoteProgram);
  if(quote.equals(spl.NATIVE_MINT)||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(program)))throw Error('token-program');
  const row=await connection.getAccountInfo(quote);
  if(!row||row.executable||!row.owner.equals(program))throw Error('account-owner');
  safeMint(spl.unpackMint(quote,row,program),true);
  const quoteAta=spl.getAssociatedTokenAddressSync(quote,user,false,program),amount=integer(input.amount);
  const {numerator,denominator}=fraction(input.slippagePercent);
  const hop=await require('./jupiter.cjs').buildHop({user,quoteMint:quote,quoteAta,amount,
    slippageBps:numerator.muln(10000).div(denominator.muln(2)).toNumber(),connection,safeMint,
    risk:input.risk,minLiquidity:input.minLiquidity,swapRecipe:input.swapRecipe,onRecipe:input.onRecipe,
    waitForBackground:input.waitForBackground,background:true});
  // Prime existing wallet/intermediate accounts and ALTs; updates then use WS.
  const touched=[...new Set([quoteAta.toBase58(),spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user).toBase58(),
    ...hop.swaps.flatMap(ix=>ix.keys).filter(k=>k.isWritable).map(k=>k.pubkey.toBase58())])];
  if(touched.length>96)throw Error('jupiter-route-rejected');
  await connection.getMultipleAccountsInfo(touched.map(k=>new web3.PublicKey(k)));
  for(const key of hop.tables)await connection.getAddressLookupTable(new web3.PublicKey(key));
  return {warmed:true,quoteMint:quote.toBase58()};
}
module.exports={warm};
