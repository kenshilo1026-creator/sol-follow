"""Confirmed native funding lineage; all state shares the funding transaction."""
import time
from chain_common.primitives import SYSTEM, pubkey
from features.funding.decoder import Funding

MAX_CHILD_CHECKS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_child_checks(root TEXT NOT NULL, wallet TEXT NOT NULL,
 signature TEXT NOT NULL, reserved REAL NOT NULL, PRIMARY KEY(root,wallet));
CREATE TABLE IF NOT EXISTS lineage_members(wallet TEXT PRIMARY KEY, root TEXT NOT NULL,
 depth INTEGER NOT NULL, parent TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lineage_members_root ON lineage_members(root);
CREATE TABLE IF NOT EXISTS lineage_consumed(root TEXT PRIMARY KEY, signature TEXT NOT NULL,
 slot INTEGER NOT NULL, wallet TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lineage_funding(event TEXT PRIMARY KEY, parent_event TEXT NOT NULL,
 root TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lineage_funding_parent ON lineage_funding(parent_event);
CREATE TABLE IF NOT EXISTS lineage_outgoing(wallet TEXT NOT NULL, signature TEXT NOT NULL,
 slot INTEGER NOT NULL, time INTEGER NOT NULL, PRIMARY KEY(wallet,signature));
CREATE TABLE IF NOT EXISTS lineage_blocked(wallet TEXT PRIMARY KEY, signature TEXT NOT NULL,
 slot INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lineage_edges(event TEXT PRIMARY KEY, signature TEXT NOT NULL,
 source TEXT NOT NULL, wallet TEXT NOT NULL, amount TEXT NOT NULL, slot INTEGER NOT NULL,
 time INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS lineage_edges_source ON lineage_edges(source,slot);
CREATE INDEX IF NOT EXISTS lineage_edges_time ON lineage_edges(time);
"""


def native_transfers(tx, config):
    # RH only propagates plain native payments, not swaps/program CPI transfers.
    if any(ix.get('programId') not in (SYSTEM, 'ComputeBudget111111111111111111111111111111')
           for ix in tx.top):
        return []
    result = []
    for path, ix in tx.instructions():
        parsed = ix.get('parsed', {})
        if '.' in path or ix.get('programId') != SYSTEM or parsed.get('type') != 'transfer':
            continue
        info = parsed.get('info', {})
        source, wallet, amount = info.get('source'), info.get('destination'), info.get('lamports')
        if (type(amount) is not int or not 0 < amount < 2**64 or source == wallet
                or source not in tx.signers or source in config.cex or wallet not in tx.keys):
            continue
        pubkey(wallet)
        result.append(Funding(f'{tx.signature}:{path}:{wallet}:lineage', tx.signature, wallet,
                              source, 'cex-lineage', amount, tx.slot, tx.time))
    return result


def blocked(c, wallet):
    return c.execute('SELECT 1 FROM lineage_blocked WHERE wallet=? UNION ALL '
        'SELECT 1 FROM lineage_members m JOIN lineage_consumed x ON x.root=m.root WHERE m.wallet=?',
        (wallet, wallet)).fetchone() is not None


def consume(c, wallet, signature, slot):
    """Atomically claim a source family before ANY launch filters or buy awaits."""
    member = c.execute('SELECT root FROM lineage_members WHERE wallet=?', (wallet,)).fetchone()
    if not member:
        return True
    root = member['root']
    if not c.execute('INSERT OR IGNORE INTO lineage_consumed VALUES (?,?,?,?)',
                     (root, signature, slot, wallet)).rowcount:
        return False
    c.execute('INSERT INTO hotlist_removals SELECT wallet,?,?,? FROM lineage_members WHERE root=? AND wallet!=? '
        'ON CONFLICT(wallet) DO UPDATE SET slot=excluded.slot,signature=excluded.signature,reason=excluded.reason '
        'WHERE excluded.slot>hotlist_removals.slot', (slot, signature, 'lineage-source-consumed', root, wallet))
    c.execute('DELETE FROM hotlist WHERE wallet IN (SELECT wallet FROM lineage_members WHERE root=?)', (root,))
    c.execute('DELETE FROM history_requests WHERE address IN (SELECT wallet FROM lineage_members WHERE root=?)', (root,))
    return True


def invalidate(c, signature):
    """Revoke descendants of invalid funding; release only a proven invalid create."""
    events = [r[0] for r in c.execute('WITH RECURSIVE invalid(event) AS ('
        'SELECT event FROM funding WHERE signature=? UNION '
        'SELECT l.event FROM lineage_funding l JOIN invalid i ON l.parent_event=i.event) '
        'SELECT event FROM invalid', (signature,))]
    affected = set()
    for event in events:
        affected.update(r[0] for r in c.execute('SELECT wallet FROM funding WHERE event=?', (event,)))
        c.execute('DELETE FROM funding WHERE event=?', (event,))
        c.execute('DELETE FROM lineage_funding WHERE event=?', (event,))
    # Buy/build failures never enter Store.invalidate; only chain failure does.
    roots = [r[0] for r in c.execute('SELECT root FROM lineage_consumed WHERE signature=?', (signature,))]
    for root in roots:
        affected.update(r[0] for r in c.execute('SELECT wallet FROM lineage_members WHERE root=?', (root,)))
    c.execute('DELETE FROM lineage_consumed WHERE signature=?', (signature,))
    c.execute("DELETE FROM hotlist_removals WHERE signature=? AND reason='lineage-source-consumed'", (signature,))
    c.execute('DELETE FROM lineage_edges WHERE signature=?', (signature,))
    # A rolled-back transfer must not be counted; persistent cap revocation is
    # conservative; ordinary funding admission cannot erase that tombstone.
    c.execute('DELETE FROM lineage_outgoing WHERE signature=?', (signature,))
    return affected


class Lineage:
    def __init__(self, store, config):
        self.store, self.config = store, config
        if config.lineage_enabled:
            with store.db('funding') as c:
                c.execute("INSERT OR IGNORE INTO lineage_members SELECT h.wallet,h.wallet,0,'' "
                    "FROM hotlist h JOIN funding f ON f.event=h.event WHERE h.expires>? AND f.provider LIKE 'cex:%'",
                    (time.time(),))
            # Existing admitted children already consumed a check before upgrade.
            # Keep every known child, even if an older version exceeded the cap.
            with store.db('funding') as c:
                c.execute("INSERT OR IGNORE INTO lineage_child_checks SELECT root,wallet,'',? "
                          "FROM lineage_members WHERE depth>0", (time.time(),))
            store.request_histories([r['wallet'] for r in store.rows('funding',
                'SELECT h.wallet FROM hotlist h JOIN lineage_members m ON m.wallet=h.wallet WHERE h.expires>?',
                (time.time(),))])

    def observe(self, tx):
        if not self.config.lineage_enabled:
            return []
        items = native_transfers(tx, self.config)
        if not items:return []
        with self.store.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            for item in items:
                # Bounded recent edges let late parent qualification replay funding.
                if not blocked(c, item.source):
                    c.execute('INSERT OR IGNORE INTO lineage_edges VALUES (?,?,?,?,?,?,?)',
                        (item.event,item.signature,item.source,item.wallet,str(item.amount),item.slot,item.time))
            for source in {item.source for item in items}:
                member = c.execute('SELECT 1 FROM lineage_members m JOIN hotlist h ON h.wallet=m.wallet '
                                   'WHERE m.wallet=? AND h.expires>? AND h.slot<? AND h.time<=?',
                                   (source, time.time(), tx.slot, tx.time)).fetchone()
                if not member or blocked(c, source):
                    continue
                # One signature is one outgoing transaction, even with several transfers.
                c.execute('INSERT OR IGNORE INTO lineage_outgoing VALUES (?,?,?,?)',
                          (source,tx.signature,tx.slot,tx.time))
                count = c.execute('SELECT count(*) FROM lineage_outgoing WHERE wallet=?', (source,)).fetchone()[0]
                if count > self.config.lineage_max_parent_transfers:
                    c.execute('INSERT OR IGNORE INTO lineage_blocked VALUES (?,?,?)', (source,tx.signature,tx.slot))
                    c.execute('INSERT INTO hotlist_removals VALUES (?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                        'slot=max(slot,excluded.slot),signature=excluded.signature,reason=excluded.reason',
                        (source,tx.slot,tx.signature,'lineage-parent-transfer-limit'))
                    c.execute('DELETE FROM hotlist WHERE wallet=?', (source,))
                    c.execute('DELETE FROM history_requests WHERE address=?', (source,))
                    c.execute('INSERT OR IGNORE INTO chain_checks VALUES (?,?,?)', (tx.signature,tx.slot,tx.time))
            c.commit()
        candidates = []
        for item in items:
            if (item.wallet in self.config.cex or
                    not self.config.min_funding <= item.amount <= self.config.max_funding or
                    tx.delta(item.wallet) < item.amount):
                continue
            with self.store.db('funding') as c:
                parent = self.parent(c, item)
            if parent:
                candidates.append(item)
        return candidates

    def parent(self, c, item):
        if blocked(c, item.source) or blocked(c, item.wallet):
            return None
        return c.execute('SELECT h.event,m.root,m.depth FROM hotlist h JOIN lineage_members m ON m.wallet=h.wallet '
            'JOIN funding f ON f.event=h.event WHERE h.wallet=? AND h.slot<? AND h.time<=? AND h.expires>? '
            'AND m.depth<?', (item.source,item.slot,item.time,time.time(),self.config.lineage_max_hops)).fetchone()

    def reserve_child_check(self, item):
        """Persist one distinct-child slot before RPC; retries never refund it."""
        with self.store.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            parent = self.parent(c, item)
            if not parent:
                c.commit()
                return False, 'lineage-parent-unavailable'
            root = parent['root']
            existing = c.execute('SELECT 1 FROM lineage_child_checks WHERE root=? AND wallet=?',
                                 (root,item.wallet)).fetchone()
            if existing:
                c.commit()
                return True, 'lineage-child-check-resumed'
            count = c.execute('SELECT count(*) FROM lineage_child_checks WHERE root=?', (root,)).fetchone()[0]
            if count >= MAX_CHILD_CHECKS:
                c.commit()
                return False, 'lineage-child-check-limit'
            c.execute('INSERT INTO lineage_child_checks VALUES (?,?,?,?)',
                      (root,item.wallet,item.signature,time.time()))
            c.commit()
            return True, 'lineage-child-check-reserved'

    def admitted(self, item):
        if not self.config.lineage_enabled:
            return
        # Re-enqueue already observed payments after their parent qualified.
        with self.store.db('funding') as c:
            member = c.execute('SELECT 1 FROM lineage_members WHERE wallet=?', (item.wallet,)).fetchone()
            if not member or blocked(c, item.wallet):
                return
            c.execute("UPDATE jobs SET state='pending',due=?,reason='lineage-replay' WHERE signature IN "
                '(SELECT signature FROM lineage_edges WHERE source=? AND slot>? AND time>=?) '
                "AND state='done'", (time.time(),item.wallet,item.slot,time.time()-self.config.backfill_age))
        # One bounded background catch-up covers admission/subscription latency.
        self.store.request_history(item.wallet)
