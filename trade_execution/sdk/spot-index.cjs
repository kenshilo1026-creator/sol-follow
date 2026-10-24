"use strict";
// Raw quote units per raw base unit, identical scale for curve and AMM routes.
const SCALE=10n**30n;
function spotIndex(quoteReserve,baseReserve){
  const q=BigInt(quoteReserve.toString()),b=BigInt(baseReserve.toString());
  if(q<=0n||b<=0n)throw Error('spot-index-unavailable');
  const value=q*SCALE/b;
  if(value<=0n)throw Error('spot-index-unavailable');
  return value.toString();
}
module.exports={SCALE,spotIndex};
