"""Non-blocking, bounded notification/audit sink with fault deduplication."""
import asyncio
from contextlib import asynccontextmanager, suppress
from chain_common.public_rpc import rpc_notices
import logging
import time
import aiohttp

log = logging.getLogger('sol-follow')


BUY_TITLES = {
    'quote sweep submitted':'報價幣換 SOL：已送出', 'quote sweep finalized':'報價幣換 SOL：已完成',
    'quote sweep deferred':'報價幣換 SOL：暫緩，保留餘額稍後重試',
    'quote sweep unknown':'報價幣換 SOL：結果未知，保留原交易核對',
    'dev exit submitted':'dev 超標：賣單已送出', 'dev exit finalized':'dev 超標：賣出已完成',
    'dev exit deferred':'dev 超標：賣出失敗，稍後重試', 'dev exit unknown':'dev 超標：賣單結果未知，保留原交易核對',
    'dev exit retry':'dev 超標：鏈上賣出失敗，準備重試', 'dev exit dry run':'dev 超標：dry 模式，沒有實際賣出',
    'target buy skipped':'跟買已跳過', 'non-SOL buy skipped':'非 SOL 跟買已跳過',
    'buy rejected':'跟買失敗（未送出）', 'buy deferred':'跟買送單結果未知',
    'buy failed on chain':'跟買鏈上失敗', 'buy fill requires review':'跟買實收異常',
    'buy status unknown after expiry; reservation retained':'跟買狀態未知（保留訂單）',
}


def message(kind, detail):
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
    if kind=='public RPC 429':
        return (f"⚠️ [SOL] 公共 RPC 429 限流\n來源: {detail['source']}"
                f"\n方法: {detail['method']}\n通道: {detail['transport']}")
    if kind not in BUY_TITLES:
        return f'[SOL] {kind}\n{detail}'[:3900]
    lines=[f"[SOL] {BUY_TITLES[kind]}"]
    fields=[('mode','模式'),('mint','代幣'),('quote_mint','報價幣'),('stage','階段'),
            ('reason','原因'),('outcome','結果'),('route','路徑'),('amount','數量（最小單位）'),('source_wallet','目標錢包'),
            ('wallet','目標錢包'),('source_signature','目標交易'),('signature','交易簽名'),('order','訂單'),
            ('sol_lamports','換回 SOL（lamports，未扣網路費／租金）')]
    for field,label in fields:
        if field=='wallet' and detail.get('source_wallet'):
            continue
        if field=='signature' and detail.get('signature')==detail.get('source_signature'):
            continue
        value=detail.get(field)
        if value is not None:
            lines.append(f'{label}: {value}')
    return '\n'.join(lines)[:3900]


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
        log.log(logging.WARNING if alert else logging.INFO, '[SOL] %s %s', kind, log_detail)
        try:
            self.queue.put_nowait((kind, detail, alert))
        except asyncio.QueueFull:
            self.dropped += 1
            log.error('[SOL] audit/notification queue full dropped=%s', self.dropped)

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
                            json={'chat_id':self.config.telegram_chat,'text':message(kind,detail)}) as response:
                            if response.status != 200:
                                log.error('[SOL] Telegram failed HTTP=%s', response.status)
                    except Exception as exc:
                        log.error('[SOL] Telegram unavailable type=%s', type(exc).__name__)
                self.queue.task_done()
