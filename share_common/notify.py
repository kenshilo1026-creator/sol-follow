"""Non-blocking, bounded notification/audit sink with fault deduplication."""
import asyncio
from contextlib import asynccontextmanager, suppress
from chain_common.public_rpc import rpc_notices
import logging
import html
import re
import time
import aiohttp

log = logging.getLogger('sol-follow')


def health_log(detail):
    """Compact journal output; the audit queue retains the full diagnostics."""
    result = {key: detail[key] for key in ('pending', 'oldest_s', 'completed', 'expired') if key in detail}
    result['funding_ws'] = detail.get('ws_connected', False)
    stream = detail.get('hotlist_stream')
    if stream is not None:
        result.update(hotlist=stream.get('addresses', 0), stream=stream.get('connected', False))
    backup = detail.get('create_backup')
    if backup is not None:
        result['backup'] = backup.get('connected', False)
    result['rpc_429'] = {
        'background': detail.get('background_public_rpc', {}).get('rate_limited', 0),
        'execution': detail.get('execution_rpc', {}).get('rate_limited', detail.get('rpc_429', 0)),
        'reads': detail.get('execution_reads', {}).get('rate_limited', 0),
    }
    market = detail.get('market_feed', {})
    if market.get('watched') or market.get('uncovered'):
        result['sell_uncovered'] = market.get('uncovered', 0)
    return result


BUY_TITLES = {
    'take profit submitted':'止盈／止損：賣單已送出',
    'take profit finalized':'止盈／止損：賣出已完成',
    'take profit deferred':'止盈／止損：暫緩，稍後重試',
    'take profit unknown':'止盈／止損：結果未知，核對原交易',
    'quote sweep submitted':'報價幣換 SOL：已送出', 'quote sweep finalized':'報價幣換 SOL：已完成',
    'quote sweep deferred':'報價幣換 SOL：暫緩，保留餘額稍後重試',
    'quote sweep unknown':'報價幣換 SOL：結果未知，保留原交易核對',
    'dev sell catch-up incomplete':'dev 賣出歷史補查未完成，部分斷線事件可能遺漏',
    'dev sell detected':'dev 已賣出：觸發剩餘持倉 100% 跟賣',
    'dev sell check stopped':'dev 賣出核對超過 10 分鐘仍失敗，停止核對',
    'dev exit submitted':'dev 保護：賣單已送出', 'dev exit finalized':'dev 保護：賣出已完成',
    'dev exit deferred':'dev 保護：賣出失敗，稍後重試', 'dev exit unknown':'dev 保護：賣單結果未知，保留原交易核對',
    'dev exit retry':'dev 保護：鏈上賣出失敗，準備重試', 'dev exit dry run':'dev 保護：dry 模式，沒有實際賣出',
    'target buy skipped':'跟買已跳過', 'non-SOL buy skipped':'非 SOL 跟買已跳過',
    'buy rejected':'跟買失敗（未送出）', 'buy deferred':'跟買送單結果未知',
    'buy failed on chain':'跟買鏈上失敗', 'buy fill requires review':'跟買實收異常',
    'buy status unknown after expiry; reservation retained':'跟買狀態未知（保留訂單）',
}


def message(kind, detail):
    if kind=='positions retained for exit management':
        mints='、'.join(detail.get('position_mints',[])) or '無'
        return (f"[SOL] 啟動後保留持倉管理\n本地持倉紀錄: {mints}"
                f"\n另需保留的舊訂單: {len(detail.get('orders',[]))} 筆"
                "\n保留原有紀錄供賣出與交易核對使用；不是新增買單。")
    if kind=='funding queue unavailable':
        code=detail.get('sqlite_name') or detail.get('type','未知')
        return (f"⚠️ [SOL] 資格工作佇列暫時無法處理\n原因: {detail.get('reason','需查看服務日誌定位')}"
                f"\n錯誤: {code}\n位置: {detail.get('location','未知')}"
                "\n本輪工作未能正常處理，稍後重試；持續失敗會延誤入金資格及跟買訊號處理。")
    if kind=='buy finalized':
        lines=['✅ [SOL][LIVE] 買入成交',f"代幣: {detail['mint']}"]
        name=detail.get('name');symbol=detail.get('symbol')
        if name or symbol:lines.append('名稱: '+(name or '')+(f' ({symbol})' if symbol else ''))
        if detail.get('launchpad'):lines.append(f"平台: {detail['launchpad']}")
        lines.append(f"買入預算: {detail['buy_sol']} SOL")
        if 'token_amount' in detail:lines.append(f"實收: {detail['token_amount']} {symbol or 'tokens'}")
        else:lines.append(f"實收（最小單位）: {detail['tokens']}")
        lines.append(f"錢包: {detail['wallet']}")
        if detail.get('dev'):lines.append(f"Dev: {detail['dev']}")
        if 'network_fee_sol' in detail:lines.append(f"網路費: {detail['network_fee_sol']} SOL")
        slot=f"成交 slot: {detail['slot']}"
        if 'slot_delta' in detail:
            slot+=('（與來源交易同 slot）' if detail['slot_delta']==0
                   else f"（較來源交易 {detail['slot_delta']:+d} slots）")
        lines.extend([slot,'交易: '+detail['transaction_url']])
        return '\n'.join(lines)[:3900]
    if kind=='service started':
        mode='Dry run' if detail['mode']=='dry' else 'live'
        return f'sol-follow 開始 ({mode})'
    if kind=='dry create detected':
        commitment='processed（尚未確認）' if detail['commitment']=='processed' else 'confirmed'
        return (f"🔎 [SOL] Dry run 偵測到 create\nLaunchpad: {detail['launchpad']}"
                f"\n代幣: {detail['mint']}\ndev（發幣錢包）: {detail['user']}"
                f"\n交易: {detail['signature']}\nSlot: {detail['slot']}"
                f"\n指令: {detail['instruction_path']}\n確認狀態: {commitment}"
                f"\n名稱: {detail['name']}\nSymbol: {detail['symbol']}"
                "\nDry run：沒有實際下單。")[:3900]
    if kind=='dev holding entry check':
        state='dev 持倉超標，代幣已排除' if detail['state']=='blocked' else 'dev 持倉無法判定，代幣已排除'
        return (f"⚠️ [SOL] {state}\n代幣: {detail['mint']}\ndev: {detail.get('creator','未知')}"
                f"\n持倉（token）: {detail.get('holding_tokens','未知')}\n上限（token）: {detail['limit_tokens']}"
                f"\n原因: {detail['reason']}\n排除紀錄跨重啟保留，不再重查。"
                + ('\n已成交跟買將觸發全數賣回報價幣。' if detail['state']=='blocked'
                   else '\n未能證實超標：停止新增跟買，已有持倉不因此自動賣出。'))
    if kind=='market cap entry check':
        state='超過市值上限，已持續排除' if detail['state']=='blocked' else '首次市值未能判定，暫不放行'
        return (f"⚠️ [SOL] {state}\n代幣: {detail['mint']}"
                f"\n首次市值（USD）: {detail.get('market_cap_usd') or '未知'}"
                f"\n上限（USD）: {detail['limit_usd']}\n原因: {detail.get('reason',detail['state'])}"
                "\n紀錄跨重啟保留；手動 reset 後才重新檢查。")
    if kind=='qualification backlog':
        reason='隊列連續 3 分鐘增加，處理速度追不上新增速度' if detail['reason']=='growing' else '最舊工作等待超過 5 分鐘'
        return (f"⚠️ [SOL] 入場／交易資格處理隊列積壓\n{reason}"
                f"\n待處理: {detail['pending']} 筆（包括尚未解碼的工作）"
                f"\n本分鐘淨增加: {detail['pending_delta']} 筆\n最舊等待: {detail['oldest_s']} 秒"
                "\n本輪積壓只通知一次，恢復後再次積壓才重新通知。")
    if kind=='funding RPC unavailable':
        return (f"⚠️ [SOL] 入金 RPC 查詢失敗\n方法: {detail['method']}"
                "\n公共 RPC 與 Alchemy 均未能取得有效結果，稍後重試。")
    if kind=='confirmation RPC unavailable':
        return (f"⚠️ [SOL] 交易確認查詢失敗\n方法: {detail['method']}"
                "\n公共 RPC 與 Alchemy 均未能取得有效結果，保留待確認紀錄，稍後重試。")
    if kind=='trading RPC unavailable':
        return (f"⚠️ [SOL] 交易 RPC 持續失敗超過 1 分鐘\n通道: {detail['lane']}"
                f"\n方法: {detail['method']}\n主、後備均未取得有效結果；已簽名交易保留待確認。")
    if kind=='market feed unavailable':
        return ("⚠️ [SOL] 賣出監聽覆蓋不足超過 1 分鐘"
                f"\n未覆蓋地址: {detail['uncovered']}\n持續重連及補查，賣出偵測可能延遲。")
    if kind=='market feed recovered':
        return '[SOL] 賣出監聽已恢復'
    if kind in ('public RPC 429','Alchemy RPC 429'):
        provider='Alchemy' if kind=='Alchemy RPC 429' else '公共'
        return (f"⚠️ [SOL] {provider} RPC 429 限流\n來源: {detail['source']}"
                f"\n方法: {detail['method']}\n通道: {detail['transport']}")
    if kind not in BUY_TITLES:
        return f'[SOL] {kind}\n{detail}'[:3900]
    lines=[f"[SOL] {BUY_TITLES[kind]}"]
    fields=[('mode','模式'),('mint','代幣'),('quote_mint','報價幣'),('stage','階段'),
            ('rule_type','規則類型'),('thresholds','規則門檻'),('gain_percent','Spot index 盈虧（%）'),
            ('received','實收報價幣（最小單位）'),('reason','原因'),
            ('fee_actual_percent','檢查費率'),('fee_limit_percent','設定上限'),('fee_parameter','對應設定'),
            ('outcome','結果'),('route','路徑'),('amount','數量（最小單位）'),('source_wallet','目標錢包'),
            ('wallet','目標錢包'),('source_signature','目標交易'),('signature','交易簽名'),('order','訂單'),
            ('sol_lamports','換回 SOL（lamports，未扣網路費／租金）')]
    for field,label in fields:
        if field=='wallet' and detail.get('source_wallet'):
            continue
        if field=='signature' and detail.get('signature')==detail.get('source_signature'):
            continue
        value=detail.get(field)
        if field=='reason' and value in ('pool-fee-limit','total-fee-limit','token-tax-limit','quote-token-tax-limit'):
            translated={'pool-fee-limit':'單一交易池費率超過上限（含協議及適用的 creator 費）',
                        'total-fee-limit':'交易路徑合計費率超過上限',
                        'token-tax-limit':'目標代幣轉帳稅率超過上限（含已排程的新稅率）',
                        'quote-token-tax-limit':'報價幣轉帳稅率超過上限（含已排程的新稅率）'}
            value=f'{translated[value]}（{value}）'
        if value is not None:
            lines.append(f'{label}: {value}')
    return '\n'.join(lines)[:3900]


def telegram_message(kind, detail):
    """RH-style copyable token addresses; other text remains literal HTML-safe text."""
    text=html.escape(message(kind,detail),quote=False)
    alphabet='1-9A-HJ-NP-Za-km-z'
    tokens={value for key in ('mint','quote_mint','base_mint','token_address','token')
            if isinstance(value:=detail.get(key),str)
            and re.fullmatch('['+alphabet+']{32,44}',value)}
    if not tokens:return text
    pattern=r'(?<!['+alphabet+'])('+'|'.join(re.escape(v) for v in sorted(tokens))+r')(?!['+alphabet+'])'
    # Format only after the existing plain-text truncation, so tags stay closed.
    return re.sub(pattern,lambda match:'<code>'+match.group(0)+'</code>',text)


class Notices:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.queue = asyncio.Queue(maxsize=1000)
        self.last = {}
        self.dropped = 0

    def emit(self, kind, detail, *, alert=False, key=None, interval=300):
        now = time.monotonic()
        if key and now-self.last.get(key, -1e12) < interval:
            return
        if key:
            self.last[key] = now
        log_detail = detail
        if kind == 'hotlist-add':
            log_detail = {'wallet': detail.get('wallet'), 'tx': detail.get('signature'),
                          'source': detail.get('provider') or detail.get('source')}
        elif kind == 'health':
            log_detail = health_log(detail)
        log.log(logging.WARNING if alert else logging.INFO, '[SOL] %s %s', kind, log_detail)
        try:
            self.queue.put_nowait((kind, detail, alert))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            log.error('[SOL] audit/notification queue full dropped=%s', self.dropped)
            return False

    @asynccontextmanager
    async def running(self):
        # Start before startup RPCs; allow queued alerts to drain on failure.
        with rpc_notices(self):
            task=asyncio.create_task(self.run())
            try:
                yield
            finally:
                try:
                    await asyncio.wait_for(self.queue.join(),timeout=10)
                except asyncio.TimeoutError:
                    log.error('[SOL] notification drain timed out pending=%s',self.queue.qsize())
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            while True:
                kind, detail, alert = await self.queue.get()
                try:
                    if self.store is not None:
                        self.store.audit(kind, detail)
                except Exception as exc:
                    log.error('[SOL] audit unavailable type=%s; runtime evidence is separate', type(exc).__name__)
                if alert and self.config.telegram_token and self.config.telegram_chat:
                    try:
                        async with session.post('https://api.telegram.org/bot'+self.config.telegram_token+'/sendMessage',
                            json={'chat_id':self.config.telegram_chat,'text':telegram_message(kind,detail),
                                  'parse_mode':'HTML'}) as response:
                            if response.status != 200:
                                log.error('[SOL] Telegram failed HTTP=%s', response.status)
                    except Exception as exc:
                        log.error('[SOL] Telegram unavailable type=%s', type(exc).__name__)
                self.queue.task_done()
