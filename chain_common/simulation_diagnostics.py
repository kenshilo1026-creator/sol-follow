"""Allowlisted simulation evidence; no raw RPC errors, URLs or program logs."""
import re
from chain_common.primitives import pubkey

ERRORS=frozenset("BlockhashNotFound AccountNotFound ProgramAccountNotFound InsufficientFundsForFee "
    "InvalidAccountForFee AlreadyProcessed AccountInUse TooManyAccountLocks InvalidAccountData "
    "InsufficientFunds InvalidInstructionData IncorrectProgramId MissingRequiredSignature "
    "AccountNotRentExempt InvalidArgument ComputationalBudgetExceeded ProgramFailedToComplete "
    "ProgramFailedToCompile InvalidSeeds InvalidAccountOwner UninitializedAccount ArithmeticOverflow "
    "InsufficientFundsForRent MaxLoadedAccountsDataSizeExceeded".split())


def sanitize(value):
    if not isinstance(value,dict):return {}
    result={}
    if value.get('reason') in ('transaction-error','missing-compute-units','invalid-compute-units'):
        result['reason']=value['reason']
    for key,maximum in {'slot':2**53-1,'units_consumed':2**53-1,'instruction_index':255,
                        'account_index':255,'custom_code':2**32-1,'anchor_number':2**32-1}.items():
        n=value.get(key)
        if type(n) is int and 0<=n<=maximum:result[key]=n
    for key in ('transaction_error','instruction_error'):
        if isinstance(value.get(key),str) and value[key] in ERRORS:result[key]=value[key]
    code=value.get('anchor_code')
    if isinstance(code,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',code):result['anchor_code']=code
    program=value.get('failed_program')
    if isinstance(program,str):
        try:result['failed_program']=str(pubkey(program))
        except (ValueError,TypeError):pass
    return result


def from_response(response):
    value=response.get('value') or {};err=value.get('err');units=value.get('unitsConsumed')
    detail={'reason':'transaction-error' if err is not None else
            'missing-compute-units' if 'unitsConsumed' not in value else 'invalid-compute-units',
            'slot':(response.get('context') or {}).get('slot'),'units_consumed':units}
    if isinstance(err,str):detail['transaction_error']=err
    if isinstance(err,dict):
        ix=err.get('InstructionError')
        if isinstance(ix,list) and len(ix)==2:
            detail['instruction_index']=ix[0]
            if isinstance(ix[1],str):detail['instruction_error']=ix[1]
            elif isinstance(ix[1],dict):detail['custom_code']=ix[1].get('Custom')
        if isinstance(err.get('InsufficientFundsForRent'),dict):
            detail.update(transaction_error='InsufficientFundsForRent',account_index=err['InsufficientFundsForRent'].get('account_index'))
    logs=value.get('logs')
    for line in logs[-40:] if isinstance(logs,list) else ():
        if not isinstance(line,str) or len(line)>1024:continue
        failed=re.match(r'^Program ([1-9A-HJ-NP-Za-km-z]{32,44}) failed:',line)
        if failed:detail.setdefault('failed_program',failed[1])
        anchor=re.search(r'Error Code: ([A-Za-z][A-Za-z0-9_]{0,63})\. Error Number: ([0-9]{1,10})\.',line)
        if anchor:detail.update(anchor_code=anchor[1],anchor_number=int(anchor[2]))
    return sanitize(detail)
