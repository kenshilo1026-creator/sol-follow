'use strict';
// Strict envelope checks around SDK 2.x decoding: Anchor otherwise ignores tails.
// Layout source: pump-fun/pump-public-docs/idl/pump.json (2026-11-06).
const pump=require('@pump-fun/pump-sdk');
function expect(ok){if(!ok)throw Error('local-create-ineligible');}
function cursor(data){
  let offset=8;
  const take=n=>{expect(Number.isSafeInteger(n)&&n>=0&&offset+n<=data.length);const at=offset;offset+=n;return at;};
  return {take,bool(){const v=data[take(1)];expect(v<=1);return !!v;},
    string(max){const n=data.readUInt32LE(take(4));expect(n<=max);take(n);},
    get left(){return data.length-offset;},get offset(){return offset;}};
}
function create(data){
  const r=cursor(data);r.string(128);r.string(64);r.string(2048);
  r.take(128+40+32);r.bool();r.bool();r.take(32+8);
  // Earlier deployments omit creator_fee_bps + holder_reward, or just holder_reward.
  expect([0,8,9,10].includes(r.left));
  if(r.left>=8)r.take(8);
  if(r.left)r.bool();
  const depth=r.left?data[r.take(1)]:0;expect(depth<=2&&r.left===0);
  return {...pump.PUMP_SDK.decodeCreateEventBc(data.subarray(8)),depth};
}
function trade(data){
  const r=cursor(data);r.take(32+8+8);r.bool();r.take(32+8+32+32+16+32+16);
  r.bool();r.take(32);r.string(64);r.bool();r.take(32);
  const shareholders=data.readUInt32LE(r.take(4));expect(shareholders<=32);r.take(shareholders*34);
  r.take(32+24);
  // holder_rewards_bps/rewards are one deployment group. V3 adds unclaimed creator fee.
  expect([0,16,24,32].includes(r.left));
  if(r.left>=16)r.take(16);
  const creatorFeeUnclaimed=r.left?data.readBigUInt64LE(r.take(8)):null;
  // Mainnet sample has a further eight zero bytes beyond the published IDL.
  // Preserve it as opaque evidence; never assign it fee/reserve semantics.
  // Caller must corroborate final reserves against receipt balances in this case.
  const opaqueZeroTail=r.left===8;
  if(opaqueZeroTail)expect(data.subarray(r.take(8)).equals(Buffer.alloc(8)));
  expect(r.left===0);
  return {...pump.PUMP_SDK.decodeTradeEventBc(data.subarray(8)),creatorFeeUnclaimed,opaqueZeroTail};
}
function advance(before,event){
  const n=x=>BigInt(x.toString()),tokens=n(event.tokenAmount),quote=n(event.quoteAmount);
  const after={vt:n(event.virtualTokenReserves),vq:n(event.virtualQuoteReserves),
    rt:n(event.realTokenReserves),rq:n(event.realQuoteReserves)};
  const sign=event.isBuy?1n:-1n;
  // Event quote_amount and reserves exclude retained protocol/creator fees.
  // Never add fee, creator_fee, buyback_fee or curve lamports to these reserves.
  expect(after.vt===before.vt-sign*tokens&&after.rt===before.rt-sign*tokens
    &&after.vq===before.vq+sign*quote&&after.rq===before.rq+sign*quote
    &&after.vt>0n&&after.vq>0n&&after.rt>0n&&after.rq>=0n);
  return after;
}
module.exports={create,trade,advance};
