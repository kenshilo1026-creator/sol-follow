"""Durable mint/route identities, never persisted prices or transactions."""
import json
import time


class SeenTokens:
    def __init__(self, store):
        self.store = store

    def remember(self, request, wallet):
        now = time.time()
        with self.store.db('funding') as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT request FROM seen_non_sol WHERE mint=?', (request['mint'],)).fetchone()
            if old:
                previous = json.loads(old['request'])
                # A create seen later must not erase a proven first-hop pool.
                if request['route'] == 'prime' and previous['route'] != 'prime':
                    request = {**previous, 'minSlot': max(previous['minSlot'], request['minSlot'])}
                elif previous['minSlot'] > request['minSlot']:
                    request = previous
            db.execute('INSERT INTO seen_non_sol VALUES (?,?,?,?) ON CONFLICT(mint) DO UPDATE SET '
                       'request=excluded.request,wallet=excluded.wallet,seen=excluded.seen',
                       (request['mint'], json.dumps(request), wallet, now))
            db.execute('INSERT INTO seen_quote_mints VALUES (?,?,?) ON CONFLICT(mint) DO UPDATE SET '
                       'program=excluded.program,seen=excluded.seen',
                       (request['quoteMint'], request['quoteProgram'], now))
            db.commit()
        return request

    def recent(self, limit=32):
        return [(json.loads(r['request']), r['wallet']) for r in self.store.rows('funding',
            'SELECT request,wallet FROM seen_non_sol ORDER BY seen DESC LIMIT ?', (limit,))]

    def recipe(self, quote):
        rows = self.store.rows('funding', 'SELECT recipe FROM quote_recipes WHERE mint=?', (quote,))
        return json.loads(rows[0]['recipe']) if rows else None

    def save_recipe(self, quote, recipe):
        # SDK independently verifies each recipe's owners, mints and path on use.
        with self.store.db('funding') as db:
            db.execute('INSERT INTO quote_recipes VALUES (?,?,?) ON CONFLICT(mint) DO UPDATE SET '
                       'recipe=excluded.recipe,updated=excluded.updated', (quote, json.dumps(recipe), time.time()))
