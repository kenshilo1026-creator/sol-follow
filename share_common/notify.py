"""Non-blocking, bounded notification/audit sink with fault deduplication."""
import asyncio
import logging
import time
import aiohttp

log = logging.getLogger('sol-follow')


BUY_TITLES = {
    'target buy skipped':'跟買已跳過', 'non-SOL buy skipped':'非 SOL 跟買已跳過',
    'buy rejected':'跟買失敗（未送出）', 'buy deferred':'跟買送單結果未知',
    'buy failed on chain':'跟買鏈上失敗', 'buy fill requires review':'跟買實收異常',
    'buy status unknown after expiry; reservation retained':'跟買狀態未知（保留訂單）',
}


def message(kind, detail):
    if kind not in BUY_TITLES:
        return f'[SOL] {kind}\n{detail}'[:3900]
    lines=[f"[SOL] {BUY_TITLES[kind]}"]
    fields=[('mode','模式'),('mint','代幣'),('quote_mint','報價幣'),('stage','階段'),
            ('reason','原因'),('outcome','結果'),('route','路徑'),('source_wallet','目標錢包'),
            ('wallet','目標錢包'),('source_signature','目標交易'),('signature','交易簽名'),('order','訂單')]
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
        log.log(logging.WARNING if alert else logging.INFO, '[SOL] %s %s', kind, detail)
        try:
            self.queue.put_nowait((kind, detail, alert))
        except asyncio.QueueFull:
            self.dropped += 1
            log.error('[SOL] audit/notification queue full dropped=%s', self.dropped)

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            while True:
                kind, detail, alert = await self.queue.get()
                try:
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
