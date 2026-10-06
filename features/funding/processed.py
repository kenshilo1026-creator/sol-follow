"""Provisional execution evidence, distinct from confirmed CEX admission."""
import asyncio
import time


class Processed:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def record(db, raw, now):
        signal = raw['_stream']
        signature = raw['transaction']['signatures'][0]
        dead = db.execute('SELECT 1 FROM dead_slots WHERE slot=?', (raw['slot'],)).fetchone()
        db.execute('INSERT OR IGNORE INTO processed_signals VALUES (?,?,?,?,?,?,?)',
                   (signature, raw['slot'], now, int(signal['live']), 'invalid' if dead else 'pending',
                    now, 'dead-slot' if dead else ''))
        row = dict(db.execute('SELECT * FROM processed_signals WHERE signature=?', (signature,)).fetchone())
        if row['slot'] != raw['slot']:
            db.execute("UPDATE processed_signals SET state='invalid',reason='signal-slot-changed' WHERE signature=?", (signature,))
            row['state'] = 'invalid'
        signal.update(received_at=row['seen'], live=bool(row['live']))
        return row

    def row(self, signature):
        rows = self.store.rows('funding', 'SELECT * FROM processed_signals WHERE signature=?', (signature,))
        return rows[0] if rows else None

    def usable(self, signature, now=None, slot=None, require_processed=False):
        row = self.row(signature)
        if not row:
            return not require_processed  # confirmed HTTP is not processed evidence
        if slot is not None and row['slot'] != slot:
            return False
        if not row['live']:
            return False
        now = time.time() if now is None else now
        return row['state'] in ('confirmed', 'finalized') or (
            row['state'] == 'pending' and row['live'] and 0 <= now-row['seen'] <= 30)

    def order_usable(self, oid, require_processed=False):
        rows = self.store.rows('trading', 'SELECT signature,slot FROM order_sources WHERE order_id=?', (oid,))
        return (bool(rows) or not require_processed) and all(
            self.usable(r['signature'],slot=r['slot'],require_processed=require_processed) for r in rows)

    def invalidate(self, signature, reason, uncertain=False):
        with self.store.db('funding') as db:
            db.execute('UPDATE processed_signals SET state=?,reason=?,due=? WHERE signature=?',
                       ('uncertain' if uncertain else 'invalid', reason, time.time()+30, signature))
        with self.store.db('trading') as db:
            db.execute('DELETE FROM votes WHERE signature=?', (signature,))

    def dead_slot(self, slot):
        with self.store.db('funding') as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO dead_slots VALUES (?,?)', (slot, time.time()))
            rows = db.execute("SELECT signature FROM processed_signals WHERE slot=? AND state IN ('pending','uncertain')", (slot,)).fetchall()
            db.execute("UPDATE processed_signals SET state='invalid',reason='dead-slot' WHERE slot=? AND state IN ('pending','uncertain')", (slot,))
            db.execute("INSERT INTO stream_state VALUES ('processed_cache_generation','1') ON CONFLICT(name) DO UPDATE SET value=CAST(value AS INTEGER)+1")
            db.commit()
        with self.store.db('trading') as db:
            db.executemany('DELETE FROM votes WHERE signature=?', [(r['signature'],) for r in rows])
        return len(rows)

    def confirmed(self, signature, state):
        with self.store.db('funding') as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM processed_signals WHERE signature=?', (signature,)).fetchone()
            if not row or row['state']=='invalid':
                db.rollback()
                return
            # Revisit via confirmed public HTTP to qualify funding / persist
            # chain-time creates. Existing trade event IDs prevent another buy.
            if row['state'] not in ('confirmed', 'finalized'):
                db.execute('DELETE FROM stream_payloads WHERE signature=?', (signature,))
                db.execute("UPDATE jobs SET state='pending',due=?,reason='confirmed-follow-up' WHERE signature=?", (time.time(), signature))
            db.execute("UPDATE processed_signals SET state=?,due=?,reason='' WHERE signature=?", (state, time.time()+5, signature))
            db.commit()

    async def reconcile_once(self, rpc, signals, notices, max_age):
        now = time.time()
        rows = self.store.rows('funding', "SELECT * FROM processed_signals WHERE state IN ('pending','confirmed','uncertain') AND due<=? ORDER BY due LIMIT 100", (now,))
        if not rows:
            return
        result = await rpc.call('getSignatureStatuses', [[r['signature'] for r in rows], {'searchTransactionHistory': True}])
        for row, status in zip(rows, result['value']):
            sig = row['signature']
            if status and (status.get('err') is not None or status.get('slot') != row['slot']):
                self.invalidate(sig, 'failed-or-different-slot')
                signals.invalidate(sig)
                notices.emit('processed signal invalidated', {'signature': sig, 'reason': 'failed-or-different-slot'}, alert=True, key='processed-invalid')
            elif status and status.get('confirmationStatus') in ('confirmed', 'finalized'):
                self.confirmed(sig, status['confirmationStatus'])
            elif now-row['seen'] > 30 and row['state'] != 'confirmed':
                self.invalidate(sig, 'confirmation-unavailable', uncertain=True)
                signals.invalidate(sig)
                if now-row['seen'] > max_age:
                    with self.store.db('funding') as db:
                        db.execute("UPDATE processed_signals SET state='unknown' WHERE signature=?", (sig,))
                notices.emit('processed signal confirmation unavailable; vote revoked', {'signature': sig}, key='processed-unknown', interval=60)
            else:
                with self.store.db('funding') as db:
                    db.execute('UPDATE processed_signals SET due=? WHERE signature=?', (now+1, sig))

    async def run(self, rpc, signals, notices, max_age):
        while True:
            try:
                await self.reconcile_once(rpc, signals, notices, max_age)
            except Exception as exc:
                notices.emit('processed reconciliation deferred', {'type': type(exc).__name__}, key='processed-reconcile', interval=60)
            await asyncio.sleep(1)
