"""Run from the sol-follow directory: python -m features.app check|stats|run."""
import argparse
import asyncio
import json
import logging
import sqlite3
from share_common.config import load
from share_common.instance import Instance
from features.strategy.positions import load_rules
from launchpads import ENABLED
from launchpads.create import SUPPORTED as CREATE_DECODERS
from trade_execution import VENUES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','stats','run'))
    args=parser.parse_args()
    config=load()
    rules=load_rules(config.root/'take_profit_rules.json')
    if args.command=='check':
        print(json.dumps({'mode':config.mode,'cex_sources':len(config.cex),'privacy_cash':bool(config.privacy_pools),
                          'launchpads':ENABLED,'execution_venues':VENUES,'trading_enabled':not config.dry_run,
                          'create_decoders':CREATE_DECODERS,'create_quote_assets':['SOL','SPL Token','Token-2022'],
                          'emergency_sell_routes':['pump_curve_to_quote','stonk_curve_to_quote','stonk_cpmm_to_quote','pump_swap_to_quote'],
                          'pending_routes':['pump_swap_buy','take_profit_sell_routes','other_quote_swap_venues'],
                          'jupiter_access':'keyless','jupiter_max_rps':0.5,
                          'threshold':config.n,'window_s':config.window,'buy_lamports':config.buy_amount,
                          'hotlist_feed':config.feed_mode,'hotlist_commitment':config.hotlist_commitment,
                          'max_target_buy_lamports':config.max_observed_buy,
                          'max_target_buy_usd_micros':config.max_observed_buy_usd_micros,
                          'ignore_target_buy_lamports':config.ignore_observed_buy,
                          'ignore_target_buy_usd_micros':config.ignore_observed_buy_usd_micros,
                          'min_target_buy_lamports':config.min_observed_buy,
                          'min_target_buy_usd_micros':config.min_observed_buy_usd_micros,
                          'max_dev_holding_tokens':str(config.max_dev_holding_tokens) if config.max_dev_holding_tokens is not None else None,
                          'max_market_cap_usd_k':str(config.max_market_cap_usd_micros/10**9),'alchemy_key_configured':bool(config.alchemy_key),
                          'http_policy':config.http_policy,'background_rpc_rps':config.history_rps,
                          'funding_history_provider':config.funding_history_provider,
                          'take_profit_rules':len(rules),'database':str(config.data)},indent=2))
        return
    if args.command=='stats':
        path=config.data/'funding.sqlite3'
        c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            c.execute('PRAGMA query_only=ON')
            print('current hotlist:',c.execute("SELECT count(*) FROM hotlist WHERE expires>strftime('%s','now')").fetchone()[0])
            print('jobs:',c.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())
            print('funding by provider:',c.execute('SELECT provider,count(*) FROM funding GROUP BY provider').fetchall())
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='launch_creates'").fetchone():
                print('creates:',c.execute('SELECT launchpad,status,count(*) FROM launch_creates GROUP BY launchpad,status').fetchall())
        finally:
            c.close()
        return
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    from features.runtime.service import Service
    instance=Instance(config.data)
    try:
        asyncio.run(Service(config).run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.error('[SOL] service stopped safely type=%s; signed orders remain durable',type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        instance.close()


if __name__=='__main__':
    main()
