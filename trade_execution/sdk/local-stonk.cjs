'use strict';
// Native create only: exact SOL budget, no wallet-balance read and no WSOL close.
// Verify the simulated buy's actual inner transfers instead of guessing a
// zero pre-balance for an ATA that another transaction could already have funded.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),BN=require('bn.js');
const risk=require('./risk.cjs');
function transfers(value,tx,buyIndex){
  const groups=value.innerInstructions;
  if(!Array.isArray(groups))throw Error('stonk-fill-rejected');
  const group=groups.filter(g=>g.index===buyIndex);
  if(group.length!==1||!Array.isArray(group[0].instructions))throw Error('stonk-fill-rejected');
  const keys=tx.message.staticAccountKeys;
  const resolve=x=>typeof x==='number'?keys[x]?.toBase58():String(x);
  const result=[];
  for(const ix of group[0].instructions){
    const program=ix.programId?String(ix.programId):resolve(ix.programIdIndex);
    if(![spl.TOKEN_PROGRAM_ID.toBase58(),spl.TOKEN_2022_PROGRAM_ID.toBase58()].includes(program))continue;
    if(ix.parsed){
      if(!['transferChecked','transferCheckedWithFee'].includes(ix.parsed.type))throw Error('stonk-fill-rejected');
      const i=ix.parsed.info,amount=i.tokenAmount?.amount;
      if(typeof amount!=='string'||!/^\d{1,20}$/.test(amount))throw Error('stonk-fill-rejected');
      result.push({program,source:i.source,dest:i.destination,mint:i.mint,amount:BigInt(amount)});
    }else{
      const data=Buffer.from(require('bs58').decode(ix.data)),a=ix.accounts.map(resolve);
      const checked=data.length===10&&data[0]===12,withFee=data.length===19&&data[0]===26&&data[1]===1;
      if((!checked&&!withFee)||a.length<4)throw Error('stonk-fill-rejected');
      result.push({program,source:a[0],mint:a[1],dest:a[2],amount:data.readBigUInt64LE(withFee?2:1)});
    }
  }
  return result;
}
function verify(value,tx,{mint,state,base,targetAta,nativeAta,amount,minOut,baseProgram}){
  const {PROGRAM,PLATFORM}=require('./stonk.cjs');
  const feeVaults=[PLATFORM,state.creator].map(owner=>web3.PublicKey.findProgramAddressSync([owner.toBuffer(),spl.NATIVE_MINT.toBuffer()],PROGRAM)[0].toBase58());
  const indices=tx.message.compiledInstructions.map((ix,i)=>tx.message.staticAccountKeys[ix.programIdIndex].equals(PROGRAM)?i:-1).filter(i=>i>=0);
  if(indices.length!==1)throw Error('stonk-fill-rejected');
  let paid=0n,received=0n,fees=0n;
  for(const move of transfers(value,tx,indices[0])){
    if(move.program===spl.TOKEN_PROGRAM_ID.toBase58()&&move.source===nativeAta.toBase58()
      &&move.mint===spl.NATIVE_MINT.toBase58()&&move.dest===state.vaultB.toBase58())paid+=move.amount;
    else if(move.program===baseProgram.toBase58()&&move.source===state.vaultA.toBase58()
      &&move.mint===mint.toBase58()&&move.dest===targetAta.toBase58())
      received+=move.amount-risk.tax(base,0,move.amount);
    else if(move.program===spl.TOKEN_PROGRAM_ID.toBase58()&&move.source===state.vaultB.toBase58()
      &&move.mint===spl.NATIVE_MINT.toBase58()&&feeVaults.includes(move.dest))fees+=move.amount;
    else throw Error('stonk-fill-rejected');
  }
  if(paid!==amount||fees>paid||received<minOut)throw Error('stonk-fill-rejected');
}
async function build(input,{connection,safeMint,finish,minimums,integer,fraction}){
  const {PLATFORM,poolState,feeSchedule,quoteDetails,buyInstruction}=require('./stonk.cjs');
  const user=new web3.PublicKey(input.wallet),mint=new web3.PublicKey(input.mint),pool=new web3.PublicKey(input.pool);
  const quote=spl.NATIVE_MINT,baseProgram=new web3.PublicKey(input.tokenProgram),quoteProgram=spl.TOKEN_PROGRAM_ID;
  if(input.quoteMint!==quote.toBase58()||input.quoteProgram!==quoteProgram.toBase58())throw Error('native-only');
  const config=new web3.PublicKey(input.createHint.config),amount=BigInt(integer(input.amount).toString());fraction(input.slippagePercent);
  // The first two values are transaction-local; the last two use the existing
  // continuously subscribed common cache (bounded RPC fallback when cold).
  const rows=await connection.getMultipleAccountsInfo([pool,mint,config,PLATFORM]);
  const state=poolState(rows[0],pool,mint,quote);
  if(!state.config.equals(config))throw Error('stonk-pool-rejected');
  const base=spl.unpackMint(mint,rows[1],baseProgram);safeMint(base,true);
  // Newly initialized transfer-fee schedules have the same rate/max in both
  // epochs. The projection deliberately encodes both at epoch zero.
  risk.checkTokenTax(input,base,0);
  const rate=feeSchedule(rows[2],rows[3],quote),details=quoteDetails(state,amount,rate,base,0);
  const metrics=risk.check(input,details.fees,[details.impact]);
  const {minOut}=minimums(new BN(amount.toString()),new BN(details.net.toString()),input.slippagePercent);
  const targetAta=spl.getAssociatedTokenAddressSync(mint,user,false,baseProgram),nativeAta=spl.getAssociatedTokenAddressSync(quote,user);
  const ixs=[spl.createAssociatedTokenAccountIdempotentInstruction(user,nativeAta,user,quote),
    spl.createAssociatedTokenAccountIdempotentInstruction(user,targetAta,user,mint,baseProgram),
    web3.SystemProgram.transfer({fromPubkey:user,toPubkey:nativeAta,lamports:amount}),spl.createSyncNativeInstruction(nativeAta),
    buyInstruction({user,pool,mint,quote,baseProgram,quoteProgram,state,targetAta,quoteAta:nativeAta,amount,minOut:BigInt(minOut.toString())})];
  const check={innerInstructions:true,verify(_accounts,value,tx){
    verify(value,tx,{user,mint,pool,state,base,targetAta,nativeAta,amount,minOut:BigInt(minOut.toString()),baseProgram});
  }};
  return {...await finish(connection,input,user,ixs,check),wallet:user.toBase58(),mint:mint.toBase58(),targetAta:targetAta.toBase58(),
    amount:amount.toString(),quotedOut:details.net.toString(),minOut:minOut.toString(),quoteIn:amount.toString(),quoteOut:amount.toString(),risk:metrics};
}
module.exports={build,verify};
