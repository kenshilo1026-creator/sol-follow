'use strict';
// Instructions only: keys stay local and submission uses the configured RPC.
const web3=require('@solana/web3.js');
const spl=require('@solana/spl-token');
const crypto=require('node:crypto');
const JUP=new web3.PublicKey('JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4');
const DEXES=['Whirlpool','Meteora DLMM'];
function reject(){throw Error('jupiter-route-rejected');}
function ix(raw,user) {
  if (!raw || !Array.isArray(raw.accounts) || typeof raw.data!=='string') reject();
  const keys=raw.accounts.map(k=>({pubkey:new web3.PublicKey(k.pubkey),isSigner:k.isSigner===true,isWritable:k.isWritable===true}));
  if(keys.some(k=>k.isSigner&&!k.pubkey.equals(user)))reject();
  return new web3.TransactionInstruction({programId:new web3.PublicKey(raw.programId),keys,data:Buffer.from(raw.data,'base64')});
}
function validate(body,{user,quoteMint,quoteAta,amount,slippageBps}) {
  if(body.inputMint!==spl.NATIVE_MINT.toBase58() || body.outputMint!==quoteMint.toBase58()
    || body.inAmount!==amount.toString() || body.swapMode!=='ExactIn'
    || body.slippageBps!==slippageBps || (body.platformFee && Number(body.platformFee.feeBps)!==0)
    || !/^[1-9][0-9]*$/.test(body.outAmount) || !/^[1-9][0-9]*$/.test(body.otherAmountThreshold))reject();
  const expected=BigInt(body.outAmount),minimum=BigInt(body.otherAmountThreshold);
  if(minimum>expected || minimum<expected*BigInt(10000-slippageBps)/10000n)reject();
  if(!Array.isArray(body.routePlan)||!body.routePlan.length||body.routePlan.length>8
    ||body.routePlan.some(r=>!DEXES.includes(r.swapInfo?.label)))reject();
  if(body.tipInstruction || body.cleanupInstruction || (body.otherInstructions||[]).length)reject();
  // With wrapping disabled, setup is restricted to idempotent user-owned ATAs.
  const setup=(body.setupInstructions||[]).map(raw=>{
    const x=ix(raw,user),k=x.keys;
    if(!x.programId.equals(spl.ASSOCIATED_TOKEN_PROGRAM_ID)||x.data.length!==1||x.data[0]!==1
      ||k.length!==6||!k[0].pubkey.equals(user)||!k[2].pubkey.equals(user)
      ||!k[4].pubkey.equals(web3.SystemProgram.programId)
      ||![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(k[5].pubkey))
      ||!k[1].pubkey.equals(spl.getAssociatedTokenAddressSync(k[3].pubkey,user,false,k[5].pubkey)))reject();
    return x;
  });
  const swap=ix(body.swapInstruction,user);
  const routes=['route','route_v2','shared_accounts_route','shared_accounts_route_v2'].map(name=>
    crypto.createHash('sha256').update('global:'+name).digest().subarray(0,8));
  if(!swap.programId.equals(JUP)||!swap.keys.some(k=>k.isSigner&&k.pubkey.equals(user))
    ||!swap.keys.some(k=>k.isWritable&&k.pubkey.equals(quoteAta))
    ||!routes.some(d=>swap.data.subarray(0,8).equals(d)))reject();
  const tables=Object.keys(body.addressesByLookupTableAddress||{});
  if(tables.length>8)reject();
  tables.forEach(k=>new web3.PublicKey(k));
  return {setup,swap,tables,expected,minimum};
}
async function discover(args,fetcher=fetch) {
  const params=new URLSearchParams({inputMint:spl.NATIVE_MINT.toBase58(),outputMint:args.quoteMint.toBase58(),
    amount:args.amount.toString(),taker:args.user.toBase58(),destinationTokenAccount:args.quoteAta.toBase58(),
    slippageBps:String(args.slippageBps),wrapAndUnwrapSol:'false',platformFeeBps:'0',
    dexes:DEXES.join(','),maxAccounts:'32'});
  let body;
  try {
    await args.waitForBackground?.();
    const response=await fetcher('https://api.jup.ag/swap/v2/build?'+params,
      {signal:AbortSignal.timeout(8000),redirect:'error'});
    if(response.status===429){
      const retry=response.headers?.get('retry-after');
      const seconds=retry===null || retry===undefined ? NaN : Number(retry);
      const delay=Number.isFinite(seconds)?seconds:(Date.parse(retry)-Date.now())/1000;
      const error=Error('jupiter-rate-limited');
      error.retryAfter=Number.isFinite(delay)?Math.max(2,delay):60;
      throw error;
    }
    if(!response.ok)throw Error();
    body=await response.json();
  }catch(error){
    if(error?.message==='jupiter-rate-limited')throw error;
    throw Error('jupiter-build-failed');
  }
  const checked=validate(body,args);
  return args.connection?require('./local-routes.cjs').recipeFrom(body,args.quoteMint):checked;
}
const recipes=new Map(),pending=new Map();
let ready=0,tail=Promise.resolve();
async function gatedDiscovery(args,fetcher){
  const previous=tail;let release;tail=new Promise(r=>{release=r;});await previous;
  let cooldown=2000;
  try{
    const delay=ready-Date.now();
    if(delay>2000){const e=Error('jupiter-rate-limited');e.retryAfter=delay/1000;throw e;}
    if(delay>0)await new Promise(r=>setTimeout(r,delay));
    return await discover(args,fetcher);
  }catch(e){if(e.message==='jupiter-rate-limited')cooldown=Math.max(cooldown,e.retryAfter*1000);throw e;}
  finally{ready=Date.now()+cooldown;release();}
}
async function buildHop(args,fetcher=fetch){
  if(!args.connection)return discover(args,fetcher); // legacy adapter tests/probes
  const key=args.quoteMint.toBase58();
  let recipe=args.swapRecipe||recipes.get(key);
  if(!recipe){
    // A foreground trade must not wait on discovery paused by that same trade.
    // Warm recipes remain usable; a cold route can be tried on a future signal.
    if(!args.background&&[...pending.values()].some(p=>p.background))throw Error('price-cache-miss');
    let p=pending.get(key);
    if(!p){p=gatedDiscovery(args,fetcher);p.background=!!args.background;pending.set(key,p);}
    try{recipe=await p;}finally{if(pending.get(key)===p)pending.delete(key);}
  }
  require('./local-routes.cjs').validateRecipe(recipe,args.quoteMint);
  recipes.delete(key);recipes.set(key,recipe);
  while(recipes.size>512)recipes.delete(recipes.keys().next().value);
  // Persist discovery even if this particular target has since graduated.
  if(args.onRecipe)args.onRecipe(key,recipe);
  return require('./local-routes.cjs').buildLocal(args,recipe);
}
module.exports={buildHop,validate,DEXES};
