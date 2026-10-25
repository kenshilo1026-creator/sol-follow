"""Hotlisted signing dev creates trigger one entry; no wallet-vote threshold."""
import json
import time

from chain_common.primitives import WSOL, pubkey
from features.audit.writer import trade_record
from features.funding.processed import Processed
from features.strategy.trade import Trade
from features.strategy.create_dev_gate import permitted as quantity_permitted
from launchpads import pump_fun, stonk
from trade_execution.route import Route
from trade_execution.stonk import StonkRoute


def create_route(item, tx):
    # The user/payer is the signing launcher. The separate creator field can be
    # an arbitrary fee beneficiary and must not lend another wallet's hotlist.
    if item.user not in tx.signers or item.signature != tx.signature or item.slot != tx.slot:
        return None
    tables=tuple(r['accountKey'] for r in tx.raw['transaction']['message'].get('addressTableLookups',[]))
    if len(tables)>8:return None
    for key in tables:pubkey(key)
    trade=Trade(item.event,item.signature,item.slot,item.time,item.user,item.mint,'create',
                0,0,tx.owner_tokens('post',item.user,item.mint),item.pool)
    if item.launchpad==pump_fun.NAME and item.program==pump_fun.PROGRAM:
        if item.pool!=pump_fun.curve_address(item.mint):return None
        return Route(trade,item.quote_mint,item.base_token_program,item.quote_token_program,'',tables,
                     kind='pump_native_curve' if item.quote_mint==WSOL else 'sol_to_pump_curve')
    if item.launchpad==stonk.NAME and item.program==stonk.PROGRAM:
        if item.pool!=stonk.pool_address(item.mint,item.quote_mint):return None
        return StonkRoute(trade,item.quote_mint,item.base_token_program,item.quote_token_program,tables,
                          kind='stonk_native_curve' if item.quote_mint==WSOL else 'sol_to_stonk_curve')
    return None


class CreateSignals:
    def __init__(self,store,config,started=None):
        self.store,self.config=store,config
        self.started=time.time() if started is None else started
        self.proofs=Processed(store)

    def prepare(self,item,tx,live,now=None):
        now=time.time() if now is None else now
        route=create_route(item,tx)
        if route is None:return None
        trade=route.trade
        processed=self.config.hotlist_commitment=='processed'
        proof=self.proofs.row(item.signature)
        backup=self.proofs.create_backup(item.signature,item.slot)
        if (tx.meta.get('err') is not None or not self.proofs.usable(item.signature,now,item.slot,
                                                                    require_processed=processed)
                or processed and not backup and (not proof or proof['state']!='pending')):
            trade_record(self.store,trade,'dev-create','blocked','source-proof-unusable')
            return None
        claimed,reason,funding=self.store.consume_dev_create(item,now,processed=processed and not backup,
            min_funding_age_seconds=self.config.min_funding_age_seconds)
        age_detail={}
        if funding:
            stamp=funding['time']
            age_detail={'funding_event':funding['event'],'funding_signature':funding['signature'],
                'funding_provider':funding['provider'],'funded_time':stamp,
                'funding_age_minutes':(now-stamp)/60 if type(stamp)==int and stamp>0 else None,
                'required_funding_age_minutes':str(self.config.buy_only_aged_minutes)}
        if not claimed:
            trade_record(self.store,trade,'dev-create','blocked',reason,age_detail)
            return None
        # Like RH on_launch, consume the hotlist before filters/builds. A stale
        # launch or a failed simulation cannot rearm a second launch/replay.
        if not live or not max(self.started,now-self.config.signal_age)<=item.time<=now:
            trade_record(self.store,trade,'dev-create','skipped','create-not-live-or-too-old')
            return None
        trade_record(self.store,trade,'dev-create','candidate','hotlisted-dev-create',
                     {'creator':item.creator,'launchpad':item.launchpad,'buy_trigger':'dev-create',**age_detail})
        return route

    def reserve(self,route):
        trade=route.trade
        if not quantity_permitted(self.store,route,self.config.max_dev_holding_tokens):return None
        if not self.proofs.usable(trade.signature,slot=trade.slot,
                                  require_processed=self.config.hotlist_commitment=='processed'):
            return None
        oid=self.store.reserve(self.config,trade.mint,trade.pool,self.config.buy_amount,
            sources=[{'wallet':trade.wallet,'signature':trade.signature,'slot':trade.slot}],check_market_cap=False,allow_pending_dev=False)
        if oid:
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL')
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT OR IGNORE INTO order_routes VALUES (?,?)',(oid,json.dumps(route.request())))
                c.execute('INSERT OR IGNORE INTO dev_sell_watches VALUES (?,?,?)',(oid,trade.wallet,trade.slot))
                c.commit()
        trade_record(self.store,trade,'dev-create','reserved' if oid else 'blocked',
                     'order-reserved' if oid else 'order-dedup-or-entry-gate',{'order':oid})
        return oid

    def invalidate(self,signature):
        proof=self.proofs.row(signature)
        if proof and proof['state']=='invalid':
            self.store.invalidate(signature)

    def prune(self,now=None):
        pass  # No rolling wallet votes in create mode.
