from features.funding.backlog import BacklogMonitor,STATE_KEY
from share_common.notify import Notices,message


def alerts(notices):
    items=[]
    while not notices.queue.empty():
        row=notices.queue.get_nowait()
        if row[2]:items.append(row)
    return items


def test_sustained_growth_alerts_once(config,store):
    notices=Notices(config,store);monitor=BacklogMonitor(store,notices)
    for tick in range(1,3):monitor.observe(tick*10,tick*60-10,tick*60)
    assert not alerts(notices)
    monitor.observe(30,170,180)
    notice=alerts(notices)
    assert len(notice)==1 and notice[0][1]['reason']=='growing'
    assert '30' in message(*notice[0][:2])
    for tick in range(4,100):monitor.observe(tick*10,170,tick*60)
    assert not alerts(notices)
    assert store.stream_state(STATE_KEY) is True


def test_stuck_work_alerts_even_without_new_arrivals(config,store):
    notices=Notices(config,store);monitor=BacklogMonitor(store,notices)
    monitor.observe(1,0,300)
    assert not alerts(notices)
    monitor.observe(1,0,360)
    notice=alerts(notices)
    assert len(notice)==1 and notice[0][1]['reason']=='stale'
    monitor.observe(1,0,1200)
    assert not alerts(notices)


def test_restart_keeps_episode_muted_and_rearms_only_after_stable_recovery(config,store):
    notices=Notices(config,store);monitor=BacklogMonitor(store,notices)
    monitor.observe(10,0,360)
    assert len(alerts(notices))==1
    monitor=BacklogMonitor(store,notices)
    monitor.observe(10,0,420)
    assert not alerts(notices)
    monitor.observe(0,None,480)
    monitor.observe(0,None,540)
    assert monitor.active
    # One bad sample resets recovery; a shrinking but old backlog is not recovered.
    monitor.observe(5,0,600)
    assert monitor.active
    for tick in (660,720,780):monitor.observe(0,None,tick)
    assert not monitor.active and store.stream_state(STATE_KEY) is False
    assert not alerts(notices)  # Recovery is local audit only.
    for tick in (1,2,3):monitor.observe(tick,780+tick*60-10,780+tick*60)
    assert len(alerts(notices))==1


def test_idle_and_short_bursts_do_not_alert(config,store):
    notices=Notices(config,store);monitor=BacklogMonitor(store,notices)
    for tick,count in enumerate((0,10,20,0,10,20,0),1):
        monitor.observe(count,tick*60-10 if count else None,tick*60)
    assert not alerts(notices)


def test_health_loop_uses_monitor_instead_of_periodic_backlog_alert(config,store,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from features.runtime.service import Service
    import features.runtime.service as module
    service=Service(config,store)
    store.enqueue(['stuck'],now=0)
    sleeps=0
    async def sleep(_):
        nonlocal sleeps
        sleeps+=1
        if sleeps>5:raise asyncio.CancelledError
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    async def scenario():
        try:
            await service.health(SimpleNamespace(received=0,added=0,connected=True,subscribed=set()),
                                 SimpleNamespace(calls=0,limited=0))
        except asyncio.CancelledError:pass
    asyncio.run(scenario())
    notices=alerts(service.notices)
    assert len(notices)==1 and notices[0][0]=='qualification backlog'
