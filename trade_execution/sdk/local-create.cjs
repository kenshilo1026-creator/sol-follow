'use strict';
// Transaction-local projections from successful, program-attributed events.
// Never publish these as RPC account snapshots or put them in AccountCache.
const web3=require('@solana/web3.js'),spl=require('@solana/spl-token'),pump=require('@pump-fun/pump-sdk');
const crypto=require('node:crypto');
const {PROGRAM,PLATFORM}=require('./stonk.cjs');
const pk=x=>new web3.PublicKey(x),disc=(ns,n)=>crypto.createHash('sha256').update(ns+':'+n).digest().subarray(0,8);
const bn=x=>BigInt(x.toString());
function requireValue(ok){if(!ok)throw Error('local-create-ineligible');}
function row(owner,data){return {owner,executable:false,lamports:0,data,rentEpoch:0};}
function putKey(d,off,key){pk(key).toBuffer().copy(d,off);}
function mintRow(input,supply,fee){
  requireValue(supply>0n&&supply<(1n<<64n)&&Number.isInteger(input.createHint.localSnapshot.decimals));
  const token=pk(input.tokenProgram),extended=token.equals(spl.TOKEN_2022_PROGRAM_ID);
  requireValue(extended||token.equals(spl.TOKEN_PROGRAM_ID));
  const data=Buffer.alloc(fee?166+4+spl.TransferFeeConfigLayout.span:82);
  spl.MintLayout.encode({mintAuthorityOption:0,mintAuthority:web3.PublicKey.default,supply,
    decimals:input.createHint.localSnapshot.decimals,isInitialized:true,freezeAuthorityOption:0,freezeAuthority:web3.PublicKey.default},data);
  if(fee){
    requireValue(extended&&Number.isInteger(fee.bps)&&fee.bps>=0&&fee.bps<=10000);
    data[165]=1;data.writeUInt16LE(spl.ExtensionType.TransferFeeConfig,166);data.writeUInt16LE(spl.TransferFeeConfigLayout.span,168);
    const rate={epoch:0n,maximumFee:BigInt(fee.max),transferFeeBasisPoints:fee.bps};
    spl.TransferFeeConfigLayout.encode({transferFeeConfigAuthority:web3.PublicKey.default,
      withdrawWithheldAuthority:web3.PublicKey.default,withheldAmount:0n,olderTransferFee:rate,newerTransferFee:rate},data.subarray(170));
  }
  return row(token,data);
}
function pumpRows(input,s,events){
  const createDisc=disc('event','CreateEvent'),tradeDisc=disc('event','TradeEvent');
  let initial,last,count=0;
  for(const data of events){
    if(data.subarray(0,8).equals(createDisc)){
      requireValue(!initial);initial=pump.PUMP_SDK.decodeCreateEventBc(data.subarray(8));
    }else if(data.subarray(0,8).equals(tradeDisc)){
      requireValue(initial);last=pump.PUMP_SDK.decodeTradeEventBc(data.subarray(8));count++;
      requireValue(last.mint.toBase58()===input.mint&&last.creator.toBase58()===s.creator
        &&pump.normalizeQuoteMint(last.quoteMint).toBase58()===input.quoteMint&&!last.mayhemMode);
    }else requireValue(false); // completion, creator changes and unknown upgrades use RPC
  }
  requireValue(initial&&count===s.tradeCount&&initial.mint.toBase58()===input.mint
    &&initial.bondingCurve.toBase58()===input.pool&&initial.user.toBase58()===s.user
    &&initial.creator.toBase58()===s.creator&&initial.tokenProgram.toBase58()===input.tokenProgram
    &&pump.normalizeQuoteMint(initial.quoteMint).toBase58()===input.quoteMint
    &&!initial.isMayhemMode&&!initial.isCashbackEnabled&&!initial.isHolderReward
    &&!s.flags.mayhem_mode&&!s.flags.cashback_enabled&&!s.flags.holder_reward);
  // Unknown create tails (including configurable fee variants) never become defaults.
  requireValue(s.flags.creator_fee_bps!==null&&!s.flags.uninterpreted_tail_hex);
  const supply=bn(initial.tokenTotalSupply),vt=bn(last?last.virtualTokenReserves:initial.virtualTokenReserves);
  const vq=bn(last?last.virtualQuoteReserves:initial.virtualQuoteReserves);
  const rt=bn(last?last.realTokenReserves:initial.realTokenReserves),rq=last?bn(last.realQuoteReserves):0n;
  requireValue(vt>0n&&vq>0n&&rt>0n&&rt<=supply&&rt<=vt&&s.decimals===6);
  const d=Buffer.alloc(125);disc('account','BondingCurve').copy(d);
  [vt,vq,rt,rq,supply].forEach((n,i)=>d.writeBigUInt64LE(n,8+i*8));
  putKey(d,49,s.creator);putKey(d,83,input.quoteMint);d.writeBigUInt64LE(bn(initial.creatorFeeBps),115);
  requireValue(bn(initial.creatorFeeBps)===BigInt(s.flags.creator_fee_bps||0));
  return new Map([[input.mint,mintRow(input,supply)],[input.pool,row(pump.PUMP_PROGRAM_ID,d)]]);
}
function stonkRows(input,s,events){
  let created=false,last,count=0;
  for(const data of events){
    if(data.subarray(0,8).equals(disc('event','PoolCreateEvent'))){
      requireValue(!created&&data.length>=104&&pk(data.subarray(8,40)).toBase58()===input.pool
        &&pk(data.subarray(40,72)).toBase58()===s.creator&&pk(data.subarray(72,104)).toBase58()===s.flags.config);
      created=true;
    }else if(data.subarray(0,8).equals(disc('event','TradeEvent'))){
      requireValue(created&&data.length===147&&pk(data.subarray(8,40)).toBase58()===input.pool
        &&data[144]<=1&&data[145]===0&&data[146]<=1);
      last=data;count++;
    }else requireValue(false);
  }
  // LaunchLab TradeEvent contains post-trade real reserves; never use the before fields.
  requireValue(created&&last&&count===s.tradeCount&&s.flags.curve_kind===0&&s.flags.config===input.createHint.config);
  const supply=BigInt(s.flags.supply_raw),total=last.readBigUInt64LE(40),va=last.readBigUInt64LE(48),vb=last.readBigUInt64LE(56);
  const ra=last.readBigUInt64LE(80),rb=last.readBigUInt64LE(88);
  requireValue(total===BigInt(s.flags.total_sell_raw)&&total<supply&&ra<total&&va>ra&&vb>0n);
  const d=Buffer.alloc(429);disc('account','PoolState').copy(d);d[18]=s.decimals;d[20]=s.flags.migration_type;
  [supply,total,va,vb,ra,rb].forEach((n,i)=>d.writeBigUInt64LE(n,21+i*8));
  const pool=pk(input.pool),mint=pk(input.mint),quote=pk(input.quoteMint);
  const vault=token=>web3.PublicKey.findProgramAddressSync([Buffer.from('pool_vault'),pool.toBuffer(),token.toBuffer()],PROGRAM)[0];
  [s.flags.config,PLATFORM,mint,quote,vault(mint),vault(quote),s.creator].forEach((k,i)=>putKey(d,141+i*32,k));
  const fee=s.flags.transfer_fee_enabled?{bps:s.flags.transfer_fee_bps,max:s.flags.maximum_fee_raw}:null;
  return new Map([[input.mint,mintRow(input,supply,fee)],[input.pool,row(PROGRAM,d)]]);
}
function project(input){
  try{
    if(input.localCreateEnabled===false)return {rows:new Map(),reason:'disabled'};
    const h=require('./create-fast.cjs').hint(input),s=h?.localSnapshot;
    if(!s)return {rows:new Map(),reason:'source-snapshot-unavailable'};
    requireValue(!input.side&&!input.prewarm&&s.version===1&&s.slot===input.minSlot
      &&s.mint===input.mint&&s.pool===input.pool&&s.quoteMint===input.quoteMint&&s.tokenProgram===input.tokenProgram
      &&typeof s.signature==='string'&&Array.isArray(s.events)&&s.events.length<=32
      &&Number.isInteger(s.tradeCount)&&s.tradeCount>=0&&s.tradeCount<=32);
    const events=s.events.map(x=>{requireValue(typeof x==='string'&&x.length<=10924);return Buffer.from(x,'base64');});
    const rows=h.launchpad==='pump.fun'?pumpRows(input,s,events):stonkRows(input,s,events);
    return {rows,reason:'source-create-and-trade-events'};
  }catch{return {rows:new Map(),reason:'source-snapshot-ineligible'};}
}
function connection(input,base){
  const projection=project(input),used=new Set();
  input.localCreate={eligible:projection.rows.size>0,reason:projection.reason,projectedAccounts:[]};
  input._localCreateMint=projection.rows.size?spl.unpackMint(pk(input.mint),projection.rows.get(input.mint),pk(input.tokenProgram)):null;
  if(!projection.rows.size)return base;
  return new Proxy(base,{get(target,key){
    if(key==='getMultipleAccountsInfo')return async(keys,config)=>{
      const common=typeof target.localCreateShared==='function'?await target.localCreateShared(keys):keys.map(()=>null);
      const missing=keys.filter((k,i)=>!projection.rows.has(k.toBase58())&&!common[i]);
      const fetched=missing.length?await target.getMultipleAccountsInfo(missing,config):[];
      let i=0;
      return keys.map((k,index)=>{
        const name=k.toBase58();if(!projection.rows.has(name))return common[index]||fetched[i++];
        used.add(name);input.localCreate.projectedAccounts=[...used];return projection.rows.get(name);
      });
    };
    const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
}
module.exports={project,connection};

function simulationCheck(input,check,safeMint){
  const expected=input._localCreateMint;
  if(!expected)return check;
  const previous=check?.addresses||[],addresses=[...previous,input.mint];
  const shape=m=>{const f=spl.getTransferFeeConfig(m);
    return f?[f.olderTransferFee,f.newerTransferFee].map(x=>[x.transferFeeBasisPoints,x.maximumFee.toString()]):null;};
  return {addresses,innerInstructions:check?.innerInstructions,verify(accounts,value,tx){
    const actual=accounts?.[previous.length],owner=pk(input.tokenProgram),mint=pk(input.mint);
    if(!actual||actual.executable||String(actual.owner)!==input.tokenProgram)throw Error('create-state-changed');
    const current=spl.unpackMint(mint,{...actual,owner,data:Buffer.from(actual.data[0],'base64')},owner);
    safeMint(current,input.createHint.launchpad==='stonk');
    if(current.supply!==expected.supply||current.decimals!==expected.decimals
      ||JSON.stringify(shape(current))!==JSON.stringify(shape(expected)))throw Error('create-state-changed');
    if(check)check.verify(accounts.slice(0,previous.length),value,tx);
  }};
}
module.exports.simulationCheck=simulationCheck;
