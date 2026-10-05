"""Non-blocking, bounded notification/audit sink with fault deduplication."""
import asyncio
import logging
import time
import aiohttp

log = logging.getLogger('sol-follow')


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
                            json={'chat_id':self.config.telegram_chat,'text':f'[SOL] {kind}\n{detail}'[:3900]}) as response:
                            if response.status != 200:
                                log.error('[SOL] Telegram failed HTTP=%s', response.status)
                    except Exception as exc:
                        log.error('[SOL] Telegram unavailable type=%s', type(exc).__name__)
                self.queue.task_done()
