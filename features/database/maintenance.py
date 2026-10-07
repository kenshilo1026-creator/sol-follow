"""Incremental retention; essential active state is never evicted for capacity."""
import asyncio
import shutil
import time


def usage(store):
    return sum(p.stat().st_size for p in store.directory.glob('*.sqlite3*') if p.is_file())


def batch(store, name, table, where, params, limit=128):
    allowed={'decisions','audit','jobs','hotlist','funding','events','votes','launch_creates','block_times','processed_signals','dead_slots','funding_activity_checks'}
    if table not in allowed:
        raise ValueError('cleanup-table-not-allowed')
    start=time.monotonic()
    with store.db(name) as c:
        c.set_progress_handler(lambda:time.monotonic()-start>0.04,1000)
        c.execute('BEGIN IMMEDIATE')
        deleted=c.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} ORDER BY rowid LIMIT ?)',
                          (*params,limit)).rowcount
        c.commit()
        return deleted


class Maintenance:
    def __init__(self,config,store,priority,notices):
        self.config,self.store,self.priority,self.notices=config,store,priority,notices
        self.limit=128
        self.pressured=False

    async def pass_once(self):
        cfg=self.config
        now=time.time()
        size=usage(self.store)
        free=shutil.disk_usage(self.store.directory).free
        pressured=size>=cfg.db_max*0.9 or free<cfg.min_disk_free or (self.pressured and size>=cfg.db_target)
        deleted=0
        tasks=[('funding','funding_activity_checks','expires<?',(now,)),
               ('audit','audit','time<?',(now-cfg.audit_retention,)),
               ('audit','decisions','time<?',(now-cfg.audit_retention,)),
               ('funding','block_times','time<?',(now-cfg.backfill_age*2,)),
               ('funding','processed_signals','seen<?',(now-max(cfg.backfill_age*2,86400),)),
               ('funding','dead_slots','time<?',(now-max(cfg.backfill_age*2,86400),)),
               ('funding','jobs',"state IN ('done','expired') AND first_seen<?",(now-max(cfg.backfill_age*2,86400),)),
               ('funding','hotlist','expires<?',(now,)),
               ('funding','funding','expires<? AND signature NOT IN (SELECT signature FROM chain_checks)',(now-cfg.window,)),
               ('funding','launch_creates',"status IN ('finalized','invalid') AND time<? AND signature NOT IN (SELECT signature FROM chain_checks)",(now-cfg.audit_retention,)),
               ('trading','votes','time<?',(now-cfg.window,)),
               ('trading','events','time<?',(now-max(cfg.backfill_age*2,cfg.window*2),))]
        if pressured:
            tasks.insert(0,('audit','audit','1=1',()))
            tasks.insert(1,('audit','decisions','1=1',()))
        for name,table,where,params in tasks:
            if self.priority.active:
                break
            try:
                deleted+=batch(self.store,name,table,where,params,self.limit)
                self.limit=min(512,self.limit+16)
            except Exception as exc:
                self.limit=max(8,self.limit//2)
                self.notices.emit('cleanup deferred',{'table':table,'type':type(exc).__name__,'next_batch':self.limit},
                                  key='cleanup-defer',interval=60)
            await asyncio.sleep(0)
        if not self.priority.active:
            for name in ('audit','funding','trading'):
                try:
                    with self.store.db(name) as c:
                        deadline=time.monotonic()+0.04
                        c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
                        c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                        c.execute('PRAGMA incremental_vacuum(64)').fetchall()
                except Exception:
                    pass
                await asyncio.sleep(0)
        if pressured:
            self.notices.emit('SQLite capacity pressure',{'bytes':size,'limit':cfg.db_max,'free':free,'deleted':deleted,
                'policy':'retaining hotlist, evidence, seen-mint routes, unfinished jobs, orders and positions; disk exhaustion needs more space'},
                alert=True,key='capacity',interval=600)
        elif self.pressured and size<cfg.db_target and free>=cfg.min_disk_free:
            self.notices.emit('SQLite capacity recovered',{'bytes':size,'free':free},alert=True)
        self.pressured=pressured or (self.pressured and size>=cfg.db_target)
        self.notices.emit('cleanup',{'bytes':size,'deleted':deleted,'next_batch':self.limit},key='cleanup-health',interval=60)
        return deleted

    async def run(self):
        while True:
            await self.pass_once()
            await asyncio.sleep(1 if self.pressured else 30)
