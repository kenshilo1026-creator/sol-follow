"""Immutable fill-average baseline in raw quote/base units, shared with SDK spot."""
import json
from chain_common.primitives import WSOL

SCALE=10**30


def fill_average(cost,tokens):
    if int(cost)<=0 or int(tokens)<=0:raise ValueError('invalid-entry-index')
    value=int(cost)*SCALE//int(tokens)
    if value<=0:raise ValueError('invalid-entry-index')
    return value


def register_fill(connection,buy,tokens,quote,registered):
    """Called in the fill transaction, or to backfill immutable historical costs.

    Never obtains a live price to manufacture an entry basis. Existing index,
    peak and registration time are retained across restarts and partial exits.
    """
    route=connection.execute('SELECT request FROM order_routes WHERE order_id=?',(buy['id'],)).fetchone()
    if not route:return
    mint=json.loads(route['request'])['quoteMint']
    if mint==WSOL:cost=int(quote)
    else:
        row=connection.execute('SELECT * FROM buy_quote_inputs WHERE order_id=?',(buy['id'],)).fetchone()
        if not row or row['quote_mint']!=mint:return
        cost=int(row['amount'])
    index=fill_average(cost,tokens)
    connection.execute('INSERT OR IGNORE INTO position_market_indexes VALUES (?,?,?,?,?,?,?,?)',
        (buy['id'],buy['mint'],buy['mode'],mint,str(index),str(index),None,registered))
