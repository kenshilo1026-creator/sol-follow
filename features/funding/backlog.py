"""Minute-sampled qualification backlog alert, once per persistent episode.

Jobs include undecoded funding candidates and transaction qualification. Counting
only admitted wallets or started activity checks would hide a blocked decoder.
"""
STATE_KEY='qualification_backlog_alert'
GROWING_SAMPLES=3
STALE_SECONDS=300
RECOVERY_SECONDS=60
RECOVERY_SAMPLES=3


class BacklogMonitor:
    def __init__(self,store,notices):
        self.store,self.notices=store,notices
        self.active=bool(store.stream_state(STATE_KEY,False))
        self.previous=store.rows('funding',"SELECT count(*) n FROM jobs WHERE state='pending'")[0]['n']
        self.growing=self.recovering=0

    def observe(self,pending,oldest,now):
        delta=pending-self.previous
        age=max(0,now-oldest) if oldest is not None and pending else 0
        self.growing=self.growing+1 if delta>0 else 0
        unhealthy=self.growing>=GROWING_SAMPLES or age>STALE_SECONDS
        if unhealthy and not self.active:
            self.store.set_stream_state(STATE_KEY,True)
            self.active=True
            self.notices.emit('qualification backlog',{
                'pending':pending,'pending_delta':delta,'oldest_s':round(age),
                'reason':'growing' if self.growing>=GROWING_SAMPLES else 'stale',
            },alert=True)
        if self.active:
            healthy=not unhealthy and delta<=0 and age<RECOVERY_SECONDS
            self.recovering=self.recovering+1 if healthy else 0
            if self.recovering>=RECOVERY_SAMPLES:
                self.store.set_stream_state(STATE_KEY,False)
                self.active=False
                self.growing=self.recovering=0
                self.notices.emit('qualification backlog recovered',{'pending':pending,'oldest_s':round(age)})
        self.previous=pending
        return delta
