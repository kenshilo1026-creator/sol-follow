"""Independent WAL databases. No network operation occurs inside a transaction."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid
from features.audit.writer import SCHEMA as DECISION_SCHEMA

FUNDING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS hotlist_removals(wallet TEXT PRIMARY KEY, slot INTEGER NOT NULL,
 signature TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funding_activity_checks(event TEXT PRIMARY KEY, updated REAL NOT NULL,
 expires INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS funding_activity_expiry ON funding_activity_checks(expires);
CREATE TABLE IF NOT EXISTS processed_signals(signature TEXT PRIMARY KEY, slot INTEGER NOT NULL,
 seen REAL NOT NULL, live INTEGER NOT NULL, state TEXT NOT NULL, due REAL NOT NULL, reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS processed_due ON processed_signals(state,due);
CREATE TABLE IF NOT EXISTS dead_slots(slot INTEGER PRIMARY KEY, time REAL NOT NULL);
CREATE TABLE IF NOT EXISTS seen_non_sol(mint TEXT PRIMARY KEY, request TEXT NOT NULL,
 wallet TEXT NOT NULL, seen REAL NOT NULL);
CREATE INDEX IF NOT EXISTS seen_non_sol_recent ON seen_non_sol(seen);
CREATE TABLE IF NOT EXISTS seen_quote_mints(mint TEXT PRIMARY KEY, program TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quote_recipes(mint TEXT PRIMARY KEY, recipe TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS funding(event TEXT PRIMARY KEY, signature TEXT, wallet TEXT,
 source TEXT, provider TEXT, amount TEXT, slot INTEGER, time INTEGER, expires INTEGER);
CREATE INDEX IF NOT EXISTS funding_wallet_time ON funding(wallet,time);
CREATE INDEX IF NOT EXISTS funding_expiry ON funding(expires);
CREATE TABLE IF NOT EXISTS hotlist(wallet TEXT PRIMARY KEY, event TEXT, slot INTEGER,
 time INTEGER, expires INTEGER, revision INTEGER);
CREATE INDEX IF NOT EXISTS hotlist_expiry ON hotlist(expires);
CREATE TABLE IF NOT EXISTS jobs(signature TEXT PRIMARY KEY, state TEXT, first_seen REAL,
 due REAL, attempts INTEGER DEFAULT 0, reason TEXT DEFAULT '', slot INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,due);
CREATE TABLE IF NOT EXISTS cursors(address TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_checks(signature TEXT PRIMARY KEY, slot INTEGER, time INTEGER);
CREATE TABLE IF NOT EXISTS launch_creates(event TEXT PRIMARY KEY, signature TEXT, mint TEXT,
 creator TEXT, user TEXT, launchpad TEXT, quote_mint TEXT, slot INTEGER, time INTEGER,
 status TEXT NOT NULL DEFAULT 'confirmed', detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS launch_creates_signature ON launch_creates(signature);
CREATE INDEX IF NOT EXISTS launch_creates_time ON launch_creates(time);
CREATE TABLE IF NOT EXISTS stream_payloads(signature TEXT PRIMARY KEY REFERENCES jobs(signature) ON DELETE CASCADE,
 raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stream_state(name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS block_times(slot INTEGER PRIMARY KEY, time INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS history_requests(address TEXT PRIMARY KEY, due REAL NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0);
'''
TRADING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS order_wallets(order_id TEXT NOT NULL, wallet TEXT NOT NULL,
 signature TEXT NOT NULL, slot INTEGER NOT NULL, PRIMARY KEY(order_id,wallet,signature));
CREATE INDEX IF NOT EXISTS order_wallets_wallet ON order_wallets(wallet,order_id);
CREATE TABLE IF NOT EXISTS token_entry_caps(mint TEXT PRIMARY KEY, signature TEXT NOT NULL,
 wallet TEXT NOT NULL, slot INTEGER NOT NULL, observed REAL NOT NULL, state TEXT NOT NULL,
 cap_usd_micros TEXT NOT NULL, threshold TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_sources(order_id TEXT, signature TEXT, slot INTEGER,
 PRIMARY KEY(order_id,signature));
CREATE TABLE IF NOT EXISTS events(event TEXT PRIMARY KEY, time INTEGER, signature TEXT);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE TABLE IF NOT EXISTS votes(mint TEXT, wallet TEXT, time INTEGER, slot INTEGER,
 amount TEXT, pool TEXT, signature TEXT, PRIMARY KEY(mint,wallet));
CREATE INDEX IF NOT EXISTS votes_time ON votes(time);
CREATE TABLE IF NOT EXISTS signals(key TEXT PRIMARY KEY, order_id TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, signal_key TEXT, mode TEXT,
 mint TEXT, pool TEXT, side TEXT, state TEXT, amount TEXT, min_out TEXT DEFAULT '0',
 signature TEXT UNIQUE, raw TEXT, last_height INTEGER, created REAL, updated REAL,
 reason TEXT DEFAULT '', rule_id TEXT DEFAULT '', quoted_out TEXT DEFAULT '0');
CREATE INDEX IF NOT EXISTS orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created);
CREATE TABLE IF NOT EXISTS positions(mint TEXT, mode TEXT, pool TEXT, amount TEXT,
 entry_quote TEXT, entry_tokens TEXT, peak TEXT DEFAULT '0', rules TEXT DEFAULT '{}',
 updated REAL, PRIMARY KEY(mint,mode));
'''
AUDIT_SCHEMA = '''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, time REAL,
 kind TEXT, detail TEXT); CREATE INDEX IF NOT EXISTS audit_time ON audit(time);'''


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.new_jobs = 0
        self.revisited_jobs = 0
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, schema in [('funding', FUNDING_SCHEMA), ('trading', TRADING_SCHEMA), ('audit', AUDIT_SCHEMA+DECISION_SCHEMA)]:
            with self.db(name) as c:
                # Must be selected before first table creation; incremental
                # reclamation avoids a full-size VACUUM temporary copy.
                if c.execute('PRAGMA page_count').fetchone()[0] == 0:
                    c.execute('PRAGMA auto_vacuum=INCREMENTAL')
                c.execute('PRAGMA journal_mode=WAL')
                c.executescript(schema)

    @contextmanager
    def db(self, name):
        c = sqlite3.connect(self.directory/(name+'.sqlite3'), timeout=0.05, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
        finally:
            if c.in_transaction:
                c.rollback()
            c.close()

    def rows(self, name, sql, params=()):
        with self.db(name) as c:
            return [dict(r) for r in c.execute(sql, params)]

    def audit(self, kind, detail):
        with self.db('audit') as c:
            c.execute('INSERT INTO audit(time,kind,detail) VALUES (?,?,?)',
                      (time.time(), kind, json.dumps(detail, separators=(',', ':'))[:4000]))

    def admit(self, item, ttl, now):
        if item.time+ttl <= now:
            return False
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            cur = c.execute('INSERT OR IGNORE INTO funding VALUES (?,?,?,?,?,?,?,?,?)',
                (item.event, item.signature, item.wallet, item.source, item.provider,
                 str(item.amount), item.slot, item.time, item.time+ttl))
            if not cur.rowcount:
                c.commit()
                return False
            # A delayed/replayed deposit before a consumed buy cannot re-admit it.
            removed=c.execute('SELECT slot FROM hotlist_removals WHERE wallet=?',(item.wallet,)).fetchone()
            if removed and item.slot<=removed['slot']:
                c.execute('INSERT OR IGNORE INTO chain_checks VALUES (?,?,?)',(item.signature,item.slot,item.time))
                c.commit()
                return False
            # Revision is an append-only event rowid; hotlist is derived state.
            c.execute('INSERT INTO hotlist VALUES (?,?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                'event=excluded.event,slot=excluded.slot,time=excluded.time,expires=excluded.expires,revision=excluded.revision '
                'WHERE excluded.slot>hotlist.slot',
                (item.wallet, item.event, item.slot, item.time, item.time+ttl, cur.lastrowid))
            c.execute('INSERT OR IGNORE INTO chain_checks VALUES (?,?,?)', (item.signature, item.slot, item.time))
            c.commit()
        return True

    def hotlist(self, now):
        return {r['wallet']: r for r in self.rows('funding', 'SELECT * FROM hotlist WHERE expires>?', (now,))}

    def record_creates(self, items):
        added=[]
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            for item in items:
                cur=c.execute('INSERT OR IGNORE INTO launch_creates VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (item.event,item.signature,item.mint,item.creator,item.user,item.launchpad,item.quote_mint,
                     item.slot,item.time,'confirmed',json.dumps(item.dict(),ensure_ascii=False,separators=(',',':'))))
                if cur.rowcount:
                    added.append(item)
                    c.execute('INSERT OR IGNORE INTO chain_checks VALUES (?,?,?)',(item.signature,item.slot,item.time))
            c.commit()
        return added

    def finalize_creates(self, signature):
        with self.db('funding') as c:
            c.execute("UPDATE launch_creates SET status='finalized' WHERE signature=? AND status='confirmed'",(signature,))

    def eligible(self, wallet, slot, when, now):
        # Retain historical funding events across renewal. Same-slot ordering
        # is conservatively excluded instead of guessed from receive order.
        return bool(self.rows('funding', 'SELECT 1 FROM funding WHERE wallet=? AND slot<? AND time<=? '
                              'AND expires>? AND slot>COALESCE((SELECT slot FROM hotlist_removals WHERE wallet=?),-1) LIMIT 1',
                              (wallet, slot, when, now, wallet)))

    def remove_hotlist(self, trade, reason):
        """Persist consumption without deleting funding evidence or a newer deposit."""
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO hotlist_removals VALUES (?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                      'slot=excluded.slot,signature=excluded.signature,reason=excluded.reason '
                      'WHERE excluded.slot>hotlist_removals.slot',
                      (trade.wallet,trade.slot,trade.signature,reason))
            c.execute('DELETE FROM hotlist WHERE wallet=? AND slot<=?',(trade.wallet,trade.slot))
            c.execute('DELETE FROM history_requests WHERE address=? AND NOT EXISTS '
                      '(SELECT 1 FROM hotlist WHERE wallet=?)',(trade.wallet,trade.wallet))
            c.commit()

    def enqueue(self, signatures, now=None):
        now = time.time() if now is None else now
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            c.executemany("INSERT OR IGNORE INTO jobs(signature,state,first_seen,due) VALUES (?,'pending',?,?)",
                          [(s, now, now) for s in dict.fromkeys(signatures)])
            changed = c.total_changes
            c.commit()
        self.new_jobs += changed
        return changed

    def due(self, limit, now, oldest=False, streamed=None):
        order = 'ASC' if oldest else 'DESC'
        lane = '' if streamed is None else (' AND '+('' if streamed else 'NOT ')+
            'EXISTS (SELECT 1 FROM stream_payloads p WHERE p.signature=jobs.signature)')
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in c.execute('SELECT * FROM jobs WHERE state=\'pending\' AND due<=? '
                        f'{lane} ORDER BY first_seen {order} LIMIT ?', (now, limit))]
            c.executemany('UPDATE jobs SET due=? WHERE signature=?', [(now+60, r['signature']) for r in rows])
            c.commit()
        return rows

    def job_result(self, sig, state, reason='', slot=0):
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE jobs SET state=?,reason=?,slot=? WHERE signature=?', (state, reason[:200], slot, sig))
            if state in ('done', 'expired'):
                c.execute('DELETE FROM stream_payloads WHERE signature=?', (sig,))
            c.commit()

    def finish_processed(self, signature, reason, slot):
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE jobs SET state='done',reason=?,slot=? WHERE signature=? AND reason!='confirmed-follow-up'",
                      (reason,slot,signature))
            c.execute('DELETE FROM stream_payloads WHERE signature=?',(signature,))
            c.commit()

    def reject_vote(self, trade):
        with self.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',(trade.event,trade.time,trade.signature))
            c.execute('DELETE FROM votes WHERE mint=? AND wallet=? AND slot<=?',(trade.mint,trade.wallet,trade.slot))
            c.commit()

    def stream_state(self, name, default=None):
        rows = self.rows('funding', 'SELECT value FROM stream_state WHERE name=?', (name,))
        return json.loads(rows[0]['value']) if rows else default

    def set_stream_state(self, name, value):
        with self.db('funding') as c:
            c.execute('INSERT OR REPLACE INTO stream_state VALUES (?,?)', (name,json.dumps(value)))

    def stream_transaction(self, raw):
        """Commit the payload and job before acknowledging progress to the feed."""
        sig = raw['transaction']['signatures'][0]
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            now = time.time()
            added = c.execute("INSERT OR IGNORE INTO jobs(signature,state,first_seen,due,slot) VALUES (?,'pending',?,?,?)",
                              (sig,now,now,raw['slot'])).rowcount
            if raw.get('_stream',{}).get('commitment')=='processed':
                from features.funding.processed import Processed
                Processed.record(c,raw,now)
            state = c.execute('SELECT state FROM jobs WHERE signature=?',(sig,)).fetchone()['state']
            if state == 'pending':
                c.execute('INSERT OR REPLACE INTO stream_payloads VALUES (?,?)',(sig,json.dumps(raw)))
            c.commit()
        self.new_jobs += added
        return added

    def stream_payload(self, signature):
        rows = self.rows('funding','SELECT raw FROM stream_payloads WHERE signature=?',(signature,))
        return json.loads(rows[0]['raw']) if rows else None

    def block_time(self, slot, value=None):
        with self.db('funding') as c:
            if value is not None:
                c.execute('INSERT OR REPLACE INTO block_times VALUES (?,?)',(slot,value))
                return value
            row = c.execute('SELECT time FROM block_times WHERE slot=?',(slot,)).fetchone()
            return row['time'] if row else None

    def request_history(self, address):
        self.request_histories([address])

    def request_histories(self, addresses):
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            c.executemany('INSERT INTO history_requests(address,due) VALUES (?,?) ON CONFLICT(address) DO UPDATE SET '
                      'revision=revision+1,due=excluded.due', [(address,time.time()) for address in addresses])
            c.commit()

    def finish_history(self, row, *, retry=False):
        with self.db('funding') as c:
            if retry:
                c.execute('UPDATE history_requests SET due=?,attempts=attempts+1 WHERE address=? AND revision=?',
                          (time.time()+min(60,2**min(row['attempts'],6)),row['address'],row['revision']))
            else:
                c.execute('DELETE FROM history_requests WHERE address=? AND revision=?',(row['address'],row['revision']))

    def retry(self, row, reason, now, max_age):
        expired = now-row['first_seen'] > max_age
        with self.db('funding') as c:
            c.execute('UPDATE jobs SET state=?,attempts=attempts+1,due=?,reason=? WHERE signature=?',
                ('expired' if expired else 'pending', now+min(60, 2**min(row['attempts'], 6)), reason[:200], row['signature']))
        return expired

    def cursor(self, address):
        rows = self.rows('funding', 'SELECT value FROM cursors WHERE address=?', (address,))
        return json.loads(rows[0]['value']) if rows else {}

    def save_cursor(self, address, value, signatures, revisit=False):
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            now = time.time()
            c.executemany("INSERT OR IGNORE INTO jobs(signature,state,first_seen,due) VALUES (?,'pending',?,?)",
                          [(s, now, now) for s in signatures])
            inserted=c.total_changes
            if revisit:
                c.executemany("UPDATE jobs SET state='pending',due=? WHERE signature=? AND state='done'",
                              [(now,s) for s in signatures])
            revisited=c.total_changes-inserted
            c.execute('INSERT INTO cursors VALUES (?,?) ON CONFLICT(address) DO UPDATE SET value=excluded.value',
                      (address, json.dumps(value)))
            c.commit()
        self.new_jobs += inserted
        self.revisited_jobs += revisited

    def vote(self, trade):
        with self.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            new = c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                            (trade.event, trade.time, trade.signature)).rowcount
            if new:
                old = c.execute('SELECT * FROM votes WHERE mint=? AND wallet=?', (trade.mint, trade.wallet)).fetchone()
                if old and old['slot'] == trade.slot and old['signature'] != trade.signature:
                    # Ambiguous ordering within a slot: revoke; a later slot can restore.
                    c.execute("UPDATE votes SET amount='0' WHERE mint=? AND wallet=?", (trade.mint, trade.wallet))
                elif not old or trade.slot > old['slot']:
                    vote_time = trade.time if trade.side == 'buy' else (old['time'] if old else 0)
                    c.execute('INSERT OR REPLACE INTO votes VALUES (?,?,?,?,?,?,?)',
                        (trade.mint, trade.wallet, vote_time, trade.slot, str(trade.remaining), trade.pool, trade.signature))
            c.commit()
        return bool(new)

    def reserve(self, config, mint, pool, amount, side='buy', rule_id='', sources=()):
        key = f'{config.strategy}:{config.mode}:{mint}'
        oid = uuid.uuid4().hex
        with self.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            if side == 'buy':
                cap=c.execute('SELECT state,cap_usd_micros FROM token_entry_caps WHERE mint=?',(mint,)).fetchone()
                limit=config.max_market_cap_usd_micros
                if (cap and (cap['state']!='allowed' or (limit and int(cap['cap_usd_micros'])>limit))) or (not cap and limit):
                    c.commit()
                    return None
                if not c.execute("INSERT OR IGNORE INTO signals VALUES (?,?,'reserved')", (key, oid)).rowcount:
                    c.commit()
                    return None
            elif c.execute("SELECT 1 FROM orders WHERE mint=? AND mode=? AND side='sell' AND state IN "
                           "('reserved','signed','submitted','unknown','confirmed')", (mint, config.mode)).fetchone():
                c.commit()
                return None
            now = time.time()
            c.execute('INSERT INTO orders(id,signal_key,mode,mint,pool,side,state,amount,created,updated,rule_id) '
                      "VALUES (?,?,?,?,?,?,'reserved',?,?,?,?)",
                      (oid, key, config.mode, mint, pool, side, str(amount), now, now, rule_id))
            c.executemany('INSERT OR IGNORE INTO order_wallets VALUES (?,?,?,?)',
                          [(oid,r['wallet'],r['signature'],r['slot']) for r in sources])
            c.executemany('INSERT OR IGNORE INTO order_sources VALUES (?,?,?)',
                          [(oid,r['signature'],r['slot']) for r in sources])
            c.commit()
        return oid

    def order(self, oid):
        return self.rows('trading', 'SELECT * FROM orders WHERE id=?', (oid,))[0]

    def update_order(self, oid, **fields):
        allowed = {'state','signature','raw','last_height','reason','min_out','quoted_out'}
        if not fields or set(fields)-allowed:
            raise ValueError('invalid-order-update')
        with self.db('trading') as c:
            c.execute('UPDATE orders SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE id=?',
                      (*fields.values(), time.time(), oid))

    def fail_order(self, oid, reason, state='failed'):
        with self.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE orders SET state=?,reason=?,updated=? WHERE id=?', (state, reason, time.time(), oid))
            c.execute('DELETE FROM signals WHERE order_id=?', (oid,))
            c.commit()

    def fill(self, oid, tokens, quote, now=None):
        if tokens <= 0 or quote <= 0:
            raise ValueError('nonpositive-fill')
        with self.db('trading') as c:
            c.execute('BEGIN IMMEDIATE')
            row = dict(c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())
            if row['state'] in ('finalized', 'dry-filled'):
                c.commit()
                return False
            now = time.time() if now is None else now
            if row['side'] == 'buy':
                if c.execute('SELECT 1 FROM positions WHERE mint=? AND mode=? AND amount!=\'0\'', (row['mint'],row['mode'])).fetchone():
                    raise ValueError('position-already-exists')
                c.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?)',
                    (row['mint'], row['mode'], row['pool'], str(tokens), str(quote), str(tokens), '0', '{}', now))
            else:
                p = dict(c.execute('SELECT * FROM positions WHERE mint=? AND mode=?', (row['mint'],row['mode'])).fetchone())
                if tokens > int(p['amount']):
                    raise ValueError('sale-exceeds-recorded-position')
                rules = json.loads(p['rules'])
                if row['rule_id']:
                    rules[row['rule_id']] = 'done'
                c.execute('UPDATE positions SET amount=?,rules=?,updated=? WHERE mint=? AND mode=?',
                    (str(int(p['amount'])-tokens), json.dumps(rules), now, row['mint'],row['mode']))
            state = 'dry-filled' if row['mode'] == 'dry' else 'finalized'
            c.execute('UPDATE orders SET state=?,updated=? WHERE id=?', (state,now,oid))
            c.execute('UPDATE signals SET state=? WHERE order_id=?', (state,oid))
            c.commit()
        return True

    def invalidate(self, signature):
        # Only a finalized error proves rollback. Missing RPC history does not.
        with self.db('funding') as c:
            c.execute('BEGIN IMMEDIATE')
            affected = [r[0] for r in c.execute('SELECT wallet FROM funding WHERE signature=?', (signature,))]
            c.execute('DELETE FROM funding WHERE signature=?', (signature,))
            c.execute("UPDATE launch_creates SET status='invalid' WHERE signature=?",(signature,))
            for wallet in affected:
                c.execute('DELETE FROM hotlist WHERE wallet=?', (wallet,))
                latest = c.execute('SELECT rowid,* FROM funding WHERE wallet=? AND slot>COALESCE('
                                   '(SELECT slot FROM hotlist_removals WHERE wallet=?),-1) ORDER BY slot DESC LIMIT 1', (wallet,wallet)).fetchone()
                if latest:
                    c.execute('INSERT INTO hotlist VALUES (?,?,?,?,?,?)', (wallet, latest['event'],latest['slot'],latest['time'],latest['expires'],latest['rowid']))
            c.execute('DELETE FROM chain_checks WHERE signature=?', (signature,))
            c.commit()
        with self.db('trading') as c:
            c.execute('DELETE FROM votes WHERE signature=?', (signature,))

