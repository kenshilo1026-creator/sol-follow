"use strict";
// Fixed labels only: exception messages, stacks, URLs and provider bodies never leave Node.
const phases=new WeakMap();
const STAGES=new Set(['build','compile-message','serialize-transaction','lookup-tables','verify-simulation',
  'getMultipleAccountsInfo','getMultipleAccountsInfoAndContext','getAccountInfo','getAccountInfoAndContext',
  'getLatestBlockhash','getAddressLookupTable','getEpochInfo','simulateTransaction','localCreateShared']);
const CODES=new Set(['ERR_ASSERTION','ERR_OUT_OF_RANGE','ERR_BUFFER_OUT_OF_BOUNDS','ERR_INVALID_ARG_TYPE',
  'ERR_INVALID_ARG_VALUE','ECONNRESET','ECONNREFUSED','ETIMEDOUT','ENOTFOUND','EAI_AGAIN',
  'UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT','UND_ERR_SOCKET']);
function mark(error,phase){
  if(error&&typeof error==='object'&&STAGES.has(phase)&&!phases.has(error))phases.set(error,phase);
  return error;
}
function at(phase,fn){
  try{const result=fn();
    return result&&typeof result.then==='function'?result.catch(e=>{throw mark(e,phase);}):result;
  }catch(e){throw mark(e,phase);}
}
function details(error){
  const category=error?.code==='ERR_ASSERTION'?'assertion-error':
    error instanceof RangeError?'range-error':error instanceof TypeError?'type-error':
    error instanceof SyntaxError?'syntax-error':error instanceof ReferenceError?'reference-error':
    error instanceof AggregateError?'aggregate-error':error instanceof Error?'error':'unknown-thrown';
  const out={category,phase:phases.get(error)||'build'};
  if(CODES.has(error?.code))out.code=error.code;
  return out;
}
function capture(input,error){
  if(input._buildEvidence)input._buildEvidence.exception=details(error);
}
module.exports={at,mark,details,capture};
