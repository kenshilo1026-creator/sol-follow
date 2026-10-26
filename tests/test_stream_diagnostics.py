"""Diagnostics explain exclusions without changing admission or using RPC."""
from dataclasses import replace
import time
import pytest
from chain_common.yellowstone import geyser_pb2 as pb
from features.funding.grpc_feed import HotlistFeed
from tests.helpers import Notices


def test_reason_preserves_original_live_predicate(config,store):
    feed=HotlistFeed(replace(config,signal_age=2),store,None,Notices(),None)
    feed.live_floor=99
    for anchor in (None,(100,1000,1001),(100,995,1001),(100,1005,1001)):
        feed.anchor=anchor
        for slot in (98,99,100,108,109):
            for now in (999,1000,1001,1002,1002.001,1003,1004):
                expected=bool(anchor and slot>=99 and anchor[0]<=slot<=anchor[0]+8
                    and 0<=now-anchor[2]<=2 and -2<=now-anchor[1]<=2)
                assert feed.live_slot(slot,now)==expected


@pytest.mark.parametrize('anchor,slot,now,reason',[
    (None,100,1001,'missing-block-anchor'),
    ((100,1000,1001),98,1001,'before-live-floor'),
    ((100,1000,1001),99,1001,'before-anchor-slot'),
    ((100,1000,1001),109,1001,'ahead-of-anchor-window'),
    ((100,1000,1001),101,1000,'anchor-receipt-in-future'),
    ((100,1002,1001),101,1004,'anchor-receipt-expired'),
    ((100,1005,1001),101,1001,'block-time-in-future'),
    ((100,1000,1001),101,1002.1,'block-time-expired'),
    ((100,1000,1001),101,1002,'live'),
])
def test_live_exclusion_reason(config,store,anchor,slot,now,reason):
    feed=HotlistFeed(replace(config,signal_age=2),store,None,Notices(),None)
    feed.live_floor=99;feed.anchor=anchor
    assert feed.live_reason(slot,now)==reason


def test_dropped_transaction_has_reason_in_health_and_audit(config,store,monkeypatch):
    import json
    from tests.test_streaming import wire
    from tests.test_native_buy import sample
    feed=HotlistFeed(config,store,None,Notices(),None)
    raw=sample();update=wire(raw)
    monkeypatch.setattr(time,'time',lambda:1001.)
    update.created_at.seconds=1000
    update.created_at.nanos=750_000_000
    feed.accept(update)
    snapshot=feed.snapshot()
    assert snapshot['drop_reasons']=={'missing-block-anchor':1}
    assert snapshot['dropped_nonlive']==1
    assert snapshot['last_transaction_slot']==raw['slot']
    assert snapshot['delivery_timing']['transaction_source_age']['p50_ms']==250
    assert snapshot['delivery_timing']['transaction_local_accept']['n']==1
    assert snapshot['last_source_age_s']==.25
    assert snapshot['last_delivery_age_s']==0
    assert not store.rows('funding','SELECT * FROM jobs')
    # Use the existing audit schema and per-signature record, no extra writes.
    rows=store.rows('audit','SELECT detail FROM decisions WHERE signature=?',
                    (raw['transaction']['signatures'][0],))
    assert any(json.loads(r['detail']).get('live_reason')=='missing-block-anchor' for r in rows)


def test_unknown_and_future_provider_timestamp_are_not_fake_zero_latency(config,store,monkeypatch):
    feed=HotlistFeed(config,store,None,Notices(),None)
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=999
    monkeypatch.setattr(time,'time',lambda:1000.)
    feed.accept(update)
    assert feed.snapshot()['last_source_age_s'] is None
    assert 'block_meta_source_age' not in feed.delivery.snapshot()
    update.created_at.seconds=1001
    feed.accept(update)
    assert feed.snapshot()['source_clock_ahead']==1
    assert feed.snapshot()['last_source_age_s']==-1
    assert 'block_meta_source_age' not in feed.delivery.snapshot()


def test_local_accept_duration_excludes_prior_delivery_delay(config,store,monkeypatch):
    feed=HotlistFeed(config,store,None,Notices(),None)
    update=pb.SubscribeUpdate()
    update.block_meta.slot=100
    update.block_meta.block_time.timestamp=990
    update.created_at.seconds=995
    clock=[0.]
    monkeypatch.setattr(time,'time',lambda:1000.)
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    original=feed._accept
    def slow(value):
        clock[0]+=.25
        original(value)
    monkeypatch.setattr(feed,'_accept',slow)
    feed.accept(update)
    stats=feed.snapshot()['delivery_timing']
    assert stats['block_meta_source_age']['p50_ms']==5000
    assert stats['block_meta_local_accept']['p50_ms']==250
