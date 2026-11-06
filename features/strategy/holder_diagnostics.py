"""Allowlisted holder-query failures; no provider URLs or raw exception text."""
import math
import re
from chain_common.rpc import RpcError

REASONS = {
    'background-rpc-read-method-required':'本地背景 RPC 未允許此查詢方法',
    'holder-timeout':'查詢逾時（包括背景排隊時間）',
    'holder-endpoint-cooling':'RPC 端點冷卻中，未送出查詢',
    'holder-http-error':'RPC HTTP 回應失敗',
    'holder-rpc-error':'RPC 拒絕查詢',
    'holder-network-error':'RPC 連線或傳輸失敗',
    'holder-rpc-failed':'RPC 查詢未取得有效結果',
    'holder-provider-not-configured':'未設定此備援通道',
    'holder-response-invalid':'回應格式或帳戶資料無法解析',
    'holder-token-program':'不支援的代幣程式',
    'holder-context-behind':'回應 slot 缺失或早於買入 slot',
    'holder-response-incomplete':'回應清單缺失或筆數超過驗證上限',
    'holder-duplicate-account':'回應包含重複帳戶',
    'holder-account-owner':'帳戶所屬程式不符',
    'holder-account-encoding':'帳戶編碼不符',
    'holder-account-layout':'帳戶長度、mint 或狀態不符',
    'holder-buy-not-visible':'回應未包含本錢包的正餘額，可能尚未索引或已賣出',
    'holder-check-error':'人氣檢查發生未分類錯誤',
}


class HolderUnavailable(ValueError):
    def __init__(self,failures):
        super().__init__('holder-count-unavailable')
        self.failures=failures


def failure(exc,provider,elapsed_ms,phase='rpc'):
    row={'provider':provider,'elapsed_ms':round(elapsed_ms,3)}
    reason='holder-rpc-failed' if phase=='rpc' else 'holder-response-invalid'
    if isinstance(exc,TimeoutError):reason='holder-timeout'
    elif isinstance(exc,ValueError) and str(exc) in REASONS:reason=str(exc)
    elif isinstance(exc,RpcError):
        text=str(exc)
        if text=='endpoint-cooling':reason='holder-endpoint-cooling'
        elif match:=re.fullmatch(r'HTTP-([1-5][0-9]{2})',text):
            reason='holder-http-error';row['http_status']=int(match[1])
        elif match:=re.fullmatch(r'getProgramAccounts:RPC-(-?[0-9]{1,10})',text):
            reason='holder-rpc-error'
            if -2**31<=int(match[1])<2**31:row['rpc_code']=int(match[1])
        elif text in ('getProgramAccounts:TimeoutError','getProgramAccounts:ServerTimeoutError',
                      'getProgramAccounts:ConnectionTimeoutError','getProgramAccounts:SocketTimeoutError'):
            reason='holder-timeout'
        elif text in ('getProgramAccounts:ClientConnectorError','getProgramAccounts:ClientConnectionError',
                      'getProgramAccounts:ServerDisconnectedError','getProgramAccounts:ClientOSError'):
            reason='holder-network-error'
    row['reason']=reason
    return row


def lines(detail):
    rows=detail.get('failures')
    if not isinstance(rows,list):return []
    result=[]
    for row in rows[:2]:
        if not isinstance(row,dict):continue
        provider=row.get('provider');reason=row.get('reason')
        if not isinstance(provider,str) or provider not in ('public','alchemy'):continue
        if not isinstance(reason,str) or reason not in REASONS:continue
        text=('公共 RPC' if provider=='public' else 'Alchemy')+': '+REASONS[reason]
        code=row.get('rpc_code');status=row.get('http_status');elapsed=row.get('elapsed_ms')
        if type(code) is int and -2**31<=code<2**31:text+=f'，RPC code {code}'
        if type(status) is int and 100<=status<=599:text+=f'，HTTP {status}'
        if type(elapsed) in (int,float) and math.isfinite(elapsed) and 0<=elapsed<=86400000:
            text+=f'（{elapsed:.1f} ms）'
        result.append(text)
    return result
