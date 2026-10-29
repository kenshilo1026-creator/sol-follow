'use strict';
// One unsigned build only. Start independent reads from decoded addresses
// while the route reads its mint/pool; never share a prepared plan across buys.
const web3=require('@solana/web3.js');
const {hint}=require('./create-fast.cjs');
function prepare(input,connection){
  if(input.prewarm||input.side==='sell'||input.side==='sweep'||!hint(input))return connection;
  if(!Array.isArray(input.lookupTables)||input.lookupTables.length>8)throw Error('invalid-tables');
  const keys=input.lookupTables.map(key=>new web3.PublicKey(key));
  const tables=new Map();let epoch;
  const capture=work=>{
    // Attach the rejection handler immediately, even if pool validation fails
    // before this independent read is consumed. Consumers still see the error.
    const result=Promise.resolve().then(work);
    void result.catch(()=>{});
    return result;
  };
  for(const key of keys){
    const name=key.toBase58();
    if(!tables.has(name))tables.set(name,capture(()=>connection.getAddressLookupTable(key)));
  }
  if((input.route==='stonk_native_curve'&&!input.localCreate?.eligible)||input.route==='sol_to_stonk_curve')
    epoch=capture(()=>connection.getEpochInfo());
  return new Proxy(connection,{get(target,key){
    if(key==='getAddressLookupTable')return (address,config)=>
      config===undefined&&tables.has(address.toBase58())?tables.get(address.toBase58()):target.getAddressLookupTable(address,config);
    if(key==='getEpochInfo'&&epoch)return config=>config===undefined?epoch:target.getEpochInfo(config);
    const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
}
module.exports={prepare};
