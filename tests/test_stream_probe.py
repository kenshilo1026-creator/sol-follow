import asyncio
import time

import pytest
from chain_common.yellowstone import geyser_pb2 as pb
from features.audit.stream_probe import Samples,hotlist_snapshot,measure


def test_snapshot_reads_only_active_addresses_without_mutating_database(store):
    with store.db('funding') as db:
        db.execute('INSERT INTO hotlist(wallet,expires) VALUES (?,?)',('active',time.time()+3600))
        db.execute('INSERT INTO hotlist(wallet,expires) VALUES (?,?)',('expired',time.time()-1))
    assert hotlist_snapshot(store.directory)=={'active'}
    assert len(store.rows('funding','SELECT * FROM hotlist'))==2


def test_probe_measures_current_update_and_separates_source_from_block_age():
    samples=Samples()
    update=pb.SubscribeUpdate()
    update.block_meta.slot=123
    update.block_meta.block_time.timestamp=98
    update.created_at.seconds=99;update.created_at.nanos=500000000
    samples.accept(update,100)
    row=samples.report(1)
    assert row['timing_latest_1000']['block_meta_source_age']['p95_ms']==500
    assert row['timing_latest_1000']['source_block_age']['p95_ms']==1500
    assert row['timing_latest_1000']['block_time_age']['p95_ms']==2000
    samples.accept(update,99)
    assert samples.report(2)['source_clock_ahead']==1
    update.ClearField('created_at')
    samples.accept(update,100)
    assert samples.report(3)['missing_timestamp']==1


@pytest.mark.asyncio
async def test_probe_is_bounded_replies_to_ping_and_closes_stream(config):
    queue=asyncio.Queue();writes=[];output=[]
    class Call:
        cancelled=False
        async def write(self,request):writes.append(request)
        async def read(self):return await queue.get()
        def cancel(self):self.cancelled=True
    call=Call()
    await queue.put(pb.SubscribeUpdate(ping=pb.SubscribeUpdatePing()))
    update=pb.SubscribeUpdate()
    update.block_meta.slot=123
    update.block_meta.block_time.timestamp=int(time.time())
    update.created_at.seconds=int(time.time())
    await queue.put(update)
    await asyncio.wait_for(measure(call,config,{'a','b'},.05,output.append),1)
    assert call.cancelled
    assert len(writes)==2 and writes[1].HasField('ping')
    assert not writes[0].HasField('from_slot')
    assert list(writes[0].transactions['hotlist_0'].account_include)==['a','b']
    assert output[-1]['final'] and output[-1]['messages']=={'ping':1,'block_meta':1}


def test_probe_error_output_does_not_expose_credentials(monkeypatch,capsys):
    from features.audit import stream_probe
    async def failed(*args):raise RuntimeError('https://provider/private-key')
    monkeypatch.setattr(stream_probe,'run',failed)
    monkeypatch.setattr(stream_probe,'load',lambda:None)
    monkeypatch.setattr('sys.argv',['stream_probe'])
    with pytest.raises(SystemExit) as raised:stream_probe.main()
    assert raised.value.code==1
    output=capsys.readouterr()
    assert 'private-key' not in output.out+output.err
    assert 'RuntimeError' in output.out
