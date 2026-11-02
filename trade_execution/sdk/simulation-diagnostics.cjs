'use strict';
// Extract structured simulation evidence only; never forward raw RPC/log text.
const errors=new Set(['BlockhashNotFound','AccountNotFound','ProgramAccountNotFound','InsufficientFundsForFee',
  'InvalidAccountForFee','AlreadyProcessed','AccountInUse','TooManyAccountLocks','InvalidAccountData',
  'InsufficientFunds','InvalidInstructionData','IncorrectProgramId','MissingRequiredSignature',
  'AccountNotRentExempt','InvalidArgument','ComputationalBudgetExceeded','ProgramFailedToComplete',
  'ProgramFailedToCompile','InvalidSeeds','InvalidAccountOwner','UninitializedAccount','ArithmeticOverflow',
  'InsufficientFundsForRent','MaxLoadedAccountsDataSizeExceeded']);
function details(response){
  const value=response?.value||{},err=value.err,units=value.unitsConsumed;
  const out={reason:err!=null?'transaction-error':units===undefined?'missing-compute-units':'invalid-compute-units'};
  const number=(key,n,max=Number.MAX_SAFE_INTEGER)=>{if(Number.isSafeInteger(n)&&n>=0&&n<=max)out[key]=n;};
  number('slot',response?.context?.slot);number('units_consumed',units);
  if(errors.has(err))out.transaction_error=err;
  const ix=err?.InstructionError;
  if(Array.isArray(ix)&&ix.length===2){number('instruction_index',ix[0],255);
    if(errors.has(ix[1]))out.instruction_error=ix[1];
    number('custom_code',ix[1]?.Custom,4294967295);
  }
  if(err&&Object.hasOwn(err,'InsufficientFundsForRent')){
    out.transaction_error='InsufficientFundsForRent';number('account_index',err.InsufficientFundsForRent?.account_index,255);
  }
  for(const line of (Array.isArray(value.logs)?value.logs:[]).slice(-40)){
    if(typeof line!=='string'||line.length>1024)continue;
    const failed=/^Program ([1-9A-HJ-NP-Za-km-z]{32,44}) failed:/.exec(line);
    if(failed&&!out.failed_program)out.failed_program=failed[1];
    const anchor=/Error Code: ([A-Za-z][A-Za-z0-9_]{0,63})\. Error Number: ([0-9]{1,10})\./.exec(line);
    if(anchor){out.anchor_code=anchor[1];number('anchor_number',Number(anchor[2]),4294967295);}
  }
  return out;
}
module.exports={details};
