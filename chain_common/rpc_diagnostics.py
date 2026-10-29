"""Bounded SDK failure metadata; never forward URLs or provider error text."""
import re

REASONS = {
    'execution-context-behind': '節點尚未追上所需 slot',
    'execution-endpoint-cooling': '端點冷卻中，未再請求',
    'execution-rate-limited': 'RPC 限流',
    'execution-timeout': 'RPC 請求逾時',
    'execution-network-failed': 'RPC 連線或傳輸失敗',
    'execution-http-unavailable': 'RPC HTTP 回應失敗',
    'execution-invalid-json': 'RPC 回應不是有效 JSON',
    'execution-wrong-chain': 'RPC 網路不符',
    'execution-genesis-unavailable': '無法驗證 RPC 網路',
    'execution-result-unavailable': 'RPC 結果缺失或格式錯誤',
    'execution-invalid-simulation': '模擬回應格式錯誤',
    'execution-rpc-unavailable': 'RPC 未取得有效結果',
}


def rpc_details(value):
    if not isinstance(value, dict):
        return {}
    method = value.get('method')
    if not isinstance(method, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', method):
        return {}
    failures = value.get('failures')
    if not isinstance(failures, list):
        return {}
    rows = []
    for item in failures[:6]:
        if not isinstance(item, dict) or not isinstance(item.get('reason'), str) or item['reason'] not in REASONS:
            continue
        row = {'reason': item['reason']}
        for key, allowed in [('provider', ('primary', 'fallback')), ('source', ('alchemy', 'configured'))]:
            if item.get(key) in allowed:
                row[key] = item[key]
        for key, low, high in [('attempt', 1, 3), ('http_status', 100, 599),
                               ('rpc_code', -2**31, 2**31-1),
                               ('required_slot', 0, 2**53-1), ('context_slot', 0, 2**53-1)]:
            number = item.get(key)
            if type(number) is int and low <= number <= high:
                row[key] = number
        rows.append(row)
    return {'method': method, 'failures': rows} if rows else {}
