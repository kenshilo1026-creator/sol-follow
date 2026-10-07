'use strict';
// All SDK HTTP and public WS connections share this credential-free reporter.
const {AsyncLocalStorage}=require('node:async_hooks');
const sinks=new AsyncLocalStorage();
const withReporter=(report,work)=>sinks.run(report,work);
function reporter(){const sink=sinks.getStore();return (method,transport)=>{
  if(sink)sink({event:'public-rpc-429',method:/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(method)?method:'unknown',transport});
};}
function rpcFetch(report,fetcher=fetch){
  return async(url,options)=>{
    let method='unknown';
    try{method=JSON.parse(options.body).method;}catch{}
    const response=await fetcher(url,{...options,signal:AbortSignal.timeout(8000)});
    if(response.status===429)report(method,'HTTP');
    else if(response.ok){
      // web3.js consumes text(), so inspect that same body without cloning it.
      const read=response.text.bind(response);
      response.text=async()=>{
        const text=await read();
        try{if([429,'429'].includes(JSON.parse(text)?.error?.code))report(method,'JSON-RPC');}catch{}
        return text;
      };
    }
    return response;
  };
}
function watchWebsocket(ws,report){
  if(!ws)return;
  ws.on('error',error=>{
    if(error?.statusCode===429||error?.status===429||/^Unexpected server response: 429$/.test(error?.message||''))
      report('connect','WS-handshake');
  });
  const call=ws.call.bind(ws);
  ws.call=async(method,...args)=>{
    try{return await call(method,...args);}
    catch(error){if([429,'429'].includes(error?.code))report(method,'WS-RPC');throw error;}
  };
}
module.exports={withReporter,reporter,rpcFetch,watchWebsocket};
