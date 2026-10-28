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
    rules=load_rules(config.root/config.take_profit_rules_path)
    if args.command=='check':
        print(json.dumps({'mode':config.mode,'cex_sources':len(config.cex),'privacy_cash':bool(config.privacy_pools),
                          'launchpads':ENABLED,'execution_venues':VENUES,'trading_enabled':not config.dry_run,
                          'create_decoders':CREATE_DECODERS,'create_quote_assets':['SOL','SPL Token','Token-2022'],
                          'emergency_sell_routes':['pump_curve_to_quote','stonk_curve_to_quote','stonk_cpmm_to_quote','pump_swap_to_quote'],
                          'pending_routes':['pump_swap_buy','other_quote_swap_venues'],
                          'jupiter_access':'keyless','jupiter_max_rps':0.5,
                          'buy_trigger':config.buy_trigger,'buy_lamports':config.buy_amount,
                          'buy_only_aged_minutes':str(config.buy_only_aged_minutes),
                          'hotlist_feed':config.feed_mode,'hotlist_commitment':config.hotlist_commitment,
                          'max_dev_holding_tokens':str(config.max_dev_holding_tokens) if config.max_dev_holding_tokens is not None else None,
                          'alchemy_key_configured':bool(config.alchemy_key),
                          'http_policy':config.http_policy,'background_rpc_rps':config.history_rps,
                          'funding_history_provider':config.funding_history_provider,
                          'funding_ws_provider':config.funding_ws_provider,
                          'stream_head_alchemy_fallback':bool(config.alchemy_key and config.funding_history_url!=config.rpc),
                          'funding_workers':config.funding_workers if config.feed_mode=='alchemy_grpc' else 1,
                          'funding_alchemy_rps':config.funding_alchemy_rps,
                          'confirmation_alchemy_fallback':bool(config.alchemy_key and config.funding_history_url!=config.rpc),
                          'execution_rpc_fallback':bool(config.execution_fallback_url),
                          'execution_rpc_isolated':True,
                          'execution_rpc_timeout_ms':int(config.execution_timeout*1000),
                          'execution_shares_funding_endpoint':any(url in (config.rpc,config.funding_history_url)
                              for url in (config.execution_rpc_url,config.execution_fallback_url) if url),
                          'market_feed_connections':len(config.market_ws_urls),
                          'take_profit_enabled':True,
                          'take_profit_poll_seconds':config.take_profit_poll_seconds,
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
        from features.database.errors import failures
        logging.error('[SOL] service stopped safely type=%s failures=%s; signed orders remain durable',
                      type(exc).__name__,failures(exc))
        raise SystemExit(1) from None
    finally:
        instance.close()


if __name__=='__main__':
    main()
