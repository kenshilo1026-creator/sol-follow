"""Read-only graduation research; never imports an executor or signs a trade."""
import argparse
import asyncio
import base64
from contextlib import contextmanager
import csv
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time

import aiohttp

from chain_common.accounts import account_data, key
from chain_common.borsh import Reader
from chain_common.primitives import SYSTEM, USDC, WSOL, ata, b58decode, discriminator, pubkey
from chain_common.rpc import Rpc, Priority, RpcError
from chain_common.transaction import Tx
from features.funding.activity_filter import qualify_activity, HistoryPending
from features.funding.decoder import decode as funding_decode
from launchpads import pump_fun, stonk
from launchpads.create import inspect as inspect_create
from share_common.config import ROOT, load
from share_common.instance import Instance
from trade_execution.route import decode_all

log = logging.getLogger('graduation-watch')
PROGRAMS = (pump_fun.PROGRAM, stonk.PROGRAM)
MIGRATIONS = {
    pump_fun.PROGRAM: {'migrate': (2, None, 3), 'migrate_v2': (2, 3, 4)},
    stonk.PROGRAM: {'migrate_to_cpswap': (1, 2, 17), 'migrate_to_amm': (1, 2, 23)},
}
LOG_NAMES = {'Migrate', 'MigrateV2', 'MigrateToCpswap', 'MigrateToAmm'}
UNSET = object()


class Unknown(RuntimeError):
    """Incomplete evidence, never a negative admission decision."""


def log_hints(program, logs):
    """Hints only. Every result is subsequently verified against a finalized tx.

    Associate Program data with its emitting invocation, not arbitrary text from
    another CPI. Failed frames and truncated/unclosed frames are discarded.
    """
    stack, found = [], []
    for line in logs or []:
        invoked = re.fullmatch(r'Program (\w+) invoke \[(\d+)\]', line)
        if invoked:
            stack.append([invoked[1], []])
            continue
        ended = re.match(r'Program (\w+) (success|failed:.*)$', line)
        if ended:
            if not stack or stack[-1][0] != ended[1]:
                stack.clear()
                continue
            _, hints = stack.pop()
            if ended[2] == 'success':
                if stack: stack[-1][1].extend(hints)
                else: found.extend(hints)
            continue
        if not stack or stack[-1][0] != program:
            continue
        if line.removeprefix('Program log: Instruction: ') in LOG_NAMES:
            stack[-1][1].append(('migration', ''))
        if not line.startswith('Program data: '):
            continue
        try:
            raw = base64.b64decode(line[14:], validate=True)
            if program == pump_fun.PROGRAM and raw[:8] == discriminator('event', 'CreateEvent'):
                r = Reader(raw[8:])
                for limit in (128, 64, 2048): r.string(limit)
                r.public_key()  # mint
                stack[-1][1].append(('create', r.public_key()))  # curve
            elif program == stonk.PROGRAM and raw[:8] == discriminator('event', 'PoolCreateEvent'):
                stack[-1][1].append(('create', key(raw, 8)))
        except (ValueError, IndexError):
            continue
    return list(dict.fromkeys(found))


def migration_instructions(tx):
    for path, ix in tx.instructions():
        program = ix.get('programId')
        if program not in MIGRATIONS: continue
        raw = b58decode(ix.get('data', ''))
        for name, (mint_index, quote_index, pool_index) in MIGRATIONS[program].items():
            expected_size = 25 if name=='migrate_to_amm' else 8
            if len(raw)!=expected_size or raw[:8] != discriminator('global', name): continue
            accounts = tx.accounts(ix)
            if len(accounts) <= max(mint_index, quote_index or 0, pool_index): continue
            mint, pool = accounts[mint_index], accounts[pool_index]
            quote = accounts[quote_index] if quote_index is not None else WSOL
            if program == pump_fun.PROGRAM:
                if pool != pump_fun.curve_address(mint): continue
            else:
                if pool != stonk.pool_address(mint, quote): continue
                if name == 'migrate_to_cpswap' and accounts[3] != stonk.PLATFORM: continue
            yield dict(event=f'{tx.signature}:{path}', signature=tx.signature, mint=mint,
                       pool=pool, quote_mint=quote, launchpad='pump.fun' if program==pump_fun.PROGRAM else 'stonk',
                       instruction=name, slot=tx.slot, time=tx.time)


def verify_migration(item, result):
    if result['context']['slot'] < item['slot']: raise Unknown('migration-account-stale')
    row = result['value']
    if item['launchpad'] == 'pump.fun':
        if not pump_fun.verify(row): raise Unknown('pump-origin-unavailable')
        raw = account_data(row, pump_fun.PROGRAM)
        if not raw[48]: raise Unknown('pump-curve-not-complete')
    else:
        if not row: raise Unknown('stonk-pool-unavailable')
        raw = account_data(row, stonk.PROGRAM)
        if len(raw) < 365: raise Unknown('stonk-layout-unavailable')
        if key(raw, 173) != stonk.PLATFORM: return False
        if (raw[:8] != discriminator('account','PoolState') or key(raw, 205) != item['mint']
                or key(raw, 237) != item['quote_mint'] or raw[17] != 2):
            raise Unknown('stonk-migration-unverified')
    return True


class Archive:
    def __init__(self, directory, hours=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'research.sqlite3'
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hints(pool TEXT PRIMARY KEY,signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queue(signature TEXT PRIMARY KEY,slot INTEGER,seen REAL,
                  state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,due REAL DEFAULT 0,reason TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS results(event TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(signature TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY,time REAL,kind TEXT,detail TEXT);
                CREATE TABLE IF NOT EXISTS funding_activity_checks(event TEXT PRIMARY KEY,updated REAL,
                  expires INTEGER,detail TEXT);
            ''')
            start = time.time()
            for k,v in {'start':start,'end':start+hours*3600 if hours is not None else None}.items():
                c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',(k,json.dumps(v)))
        self.start, self.end = self.setting('start'), self.setting('end')

    @contextmanager
    def db(self, name=None):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally: c.close()

    def rows(self, name, sql, params=()):
        with self.db() as c: return [dict(r) for r in c.execute(sql, params)]

    def setting(self, name, value=UNSET):
        with self.db() as c:
            if value is not UNSET:
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(name,json.dumps(value)))
            row = c.execute('SELECT value FROM settings WHERE key=?',(name,)).fetchone()
        return json.loads(row[0]) if row else None

    def coverage(self, kind, detail=''):
        with self.db() as c:
            c.execute('INSERT INTO coverage(time,kind,detail) VALUES (?,?,?)',(time.time(),kind,detail))

    def enqueue(self, signature, slot):
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO queue(signature,slot,seen) VALUES (?,?,?)',(signature,slot,time.time()))

    def save_result(self, value):
        with self.db() as c:
            c.execute('INSERT OR REPLACE INTO results VALUES (?,?)',(value['event'],json.dumps(value,ensure_ascii=False)))


class Reads:
    """One paced HTTP stream with a per-token budget and durable public receipts."""
    def __init__(self, rpc, archive, rps, budget):
        self.rpc, self.archive, self.interval = rpc, archive, 1/rps
        self.maximum, self.remaining, self.next = budget, budget, 0

    def reset(self): self.remaining = self.maximum

    async def call(self, method, params):
        if method not in {'getGenesisHash','getAccountInfo','getSignaturesForAddress','getTransaction',
                          'getBlockTime','getTokenAccountsByOwner'}:
            raise ValueError('research-read-only-rpc')
        if self.remaining <= 0: raise Unknown('per-token-rpc-budget-exhausted')
        self.remaining -= 1
        await asyncio.sleep(max(0,self.next-time.monotonic()))
        self.next = time.monotonic()+self.interval
        return await self.rpc.call(method,params)

    async def transaction(self, signature):
        rows = self.archive.rows('', 'SELECT body FROM receipts WHERE signature=?',(signature,))
        if rows: return json.loads(rows[0]['body'])
        raw = await self.call('getTransaction',[signature,{'encoding':'jsonParsed','commitment':'finalized',
                                                         'maxSupportedTransactionVersion':1}])
        if raw is None: raise Unknown('transaction-not-indexed')
        if raw['transaction']['signatures'][0] != signature: raise Unknown('receipt-signature-mismatch')
        with self.archive.db() as c:
            c.execute('INSERT OR REPLACE INTO receipts VALUES (?,?)',(signature,json.dumps(raw)))
        return raw


async def find_creation(item, reads, archive, pages):
    hints = archive.rows('', 'SELECT signature FROM hints WHERE pool=?',(item['pool'],))
    signatures = [r['signature'] for r in hints]
    if not signatures:
        before = None
        for _ in range(pages):
            options = {'limit':1000,'commitment':'finalized','minContextSlot':item['slot']}
            if before: options['before'] = before
            rows = await reads.call('getSignaturesForAddress',[item['pool'],options])
            if not isinstance(rows,list): raise Unknown('invalid-pool-history')
            if len(rows) < 1000:
                signatures = [r['signature'] for r in reversed(rows) if r.get('err') is None][:5]
                break
            before = rows[-1]['signature']
    for sig in signatures:
        raw = await reads.transaction(sig)
        tx = Tx(raw)
        for created in inspect_create(tx).creates:
            if (created.mint == item['mint'] and created.pool == item['pool']
                    and created.launchpad == item['launchpad'] and created.slot <= item['slot']):
                return created, tx
    raise Unknown('original-create-not-found-or-not-decodable-within-history-budget')


async def history(address, since, at_slot, reads, pages):
    before, found, complete = None, [], False
    for _ in range(pages):
        options = {'limit':100,'commitment':'finalized','minContextSlot':at_slot}
        if before: options['before'] = before
        rows = await reads.call('getSignaturesForAddress',[address,options])
        if not isinstance(rows,list): raise Unknown('invalid-wallet-history')
        for r in rows:
            if type(r.get('blockTime')) is not int: raise Unknown('history-time-unavailable')
        found.extend(r for r in rows if r['slot'] <= at_slot and r['blockTime'] >= since)
        if len(rows)<100 or any(r['blockTime']<since for r in rows):
            complete=True
            break
        before = rows[-1]['signature']
    return found, complete


async def admission(created, reads, archive, config, pages):
    wallet = created.creator
    if not pubkey(wallet).is_on_curve(): return {'status':'blocked','reason':'wallet-off-curve'}
    # The historical wallet account data is not in getTransaction. Record this
    # current check explicitly; never call it an archival account-state proof.
    snapshot = await reads.call('getAccountInfo',[wallet,{'encoding':'base64','commitment':'finalized',
                                                        'minContextSlot':created.slot}])
    row = snapshot['value']
    if not row or row['owner']!=SYSTEM or row.get('executable') or row['data']!=['','base64']:
        return {'status':'unknown','reason':'historical-system-wallet-state-unavailable'}
    since = created.time-config.hotlist_ttl
    owner_history, complete = await history(wallet,since,created.slot,reads,pages)
    for entry in owner_history:
        if entry['slot']==created.slot and entry['signature']!=created.signature:
            same_slot=await reads.transaction(entry['signature'])
            if wallet in {r['pubkey'] for r in same_slot['transaction']['message']['accountKeys'] if r.get('signer')}:
                return {'status':'unknown','reason':'same-slot-wallet-activity-order-unknown'}
    # Existing non-ATA USDC accounts are also searched; already closed accounts
    # cannot be enumerated from current state, so missing funding is UNKNOWN.
    accounts = await reads.call('getTokenAccountsByOwner',[wallet,{'mint':USDC},
                                {'encoding':'base64','commitment':'finalized','minContextSlot':created.slot}])
    addresses = list(dict.fromkeys([ata(wallet,USDC),*(r['pubkey'] for r in accounts['value'])]))
    candidates = {r['signature']:r for r in owner_history if r.get('err') is None}
    for addr in addresses[:8]:
        token_history, covered = await history(addr,since,created.slot,reads,pages)
        complete &= covered
        candidates.update((r['signature'],r) for r in token_history if r.get('err') is None)
    complete &= len(addresses)<=8
    declined=[]; newer=[]
    for entry in sorted(candidates.values(),key=lambda r:r['slot'],reverse=True):
        raw = await reads.transaction(entry['signature'])
        tx = Tx(raw)
        if tx.slot >= created.slot or tx.time > created.time: continue
        for item in funding_decode(tx,config):
            if item.wallet!=wallet or item.time+config.hotlist_ttl<=created.time: continue
            allowed, reason = True, 'privacy-cash-proof'
            if item.provider.startswith('cex:'):
                for _ in range(20):
                    try:
                        allowed, reason = await qualify_activity(item,reads,archive)
                        break
                    except HistoryPending as exc:
                        if str(exc)!='wallet-history-check-incomplete': raise
                else: raise Unknown('wallet-activity-budget-exhausted')
            if not allowed:
                declined.append({'signature':item.signature,'reason':reason})
                continue
            # Funding eligibility alone is insufficient if an intervening buy
            # would already have removed this wallet. Dynamic USD checks stay
            # unknown instead of being reconstructed with today's price.
            for later in newer:
                if not item.slot < later.slot < created.slot: continue
                for route in decode_all(later):
                    if route.trade.wallet!=wallet or route.trade.side!='buy': continue
                    if route.quote_mint!=WSOL or route.observed_mint!=WSOL:
                        return {'status':'unknown','reason':'intervening-non-sol-buy-needs-historical-price','funding':item.dict()}
                    paid=route.trade.quote
                    if paid<config.ignore_observed_buy: continue
                    if paid<config.min_observed_buy or route.observed_amount>config.max_observed_buy:
                        return {'status':'blocked','reason':'intervening-buy-removes-hotlist','funding':item.dict()}
            if not complete:
                return {'status':'unknown','reason':'intervening-history-incomplete','funding':item.dict()}
            return {'status':'pass','reason':reason,'funding':item.dict(),
                    'wallet_account_check':'current-finalized-snapshot-not-historical-state'}
        newer.append(tx)
    return {'status':'unknown','reason':'no-qualified-funding-found-in-covered-wallet-and-usdc-history',
            'declined_funding':declined,'history_page_coverage':complete,
            'closed_usdc_accounts_covered':False}


def creation_buy(created, tx, config):
    routes = [r for r in decode_all(tx) if r.trade.mint==created.mint and r.trade.side=='buy']
    dev = [r for r in routes if r.trade.wallet==created.creator]
    result = {'status':'pass' if dev else 'blocked','reason':'supported-dev-create-buy' if dev else 'no-supported-dev-buy-in-create-tx',
              'routes':[dict(wallet=r.trade.wallet,route=r.kind,quote_mint=r.quote_mint,
                             paid_raw=str(r.trade.quote),budget_raw=str(r.observed_amount)) for r in routes]}
    if dev:
        route=dev[0]
        if route.quote_mint!=WSOL or route.observed_mint!=WSOL:
            result.update(status='unknown',reason='supported-route-but-historical-usd-amount-gate-unavailable')
        elif route.trade.quote<max(config.ignore_observed_buy,config.min_observed_buy):
            result.update(status='blocked',reason='dev-buy-below-minimum')
        elif route.observed_amount>config.max_observed_buy:
            result.update(status='blocked',reason='dev-buy-above-maximum')
    return result


async def analyse(item, reads, archive, config, pages):
    result={**item,'dev':'','candidate':'unknown','create_decode':'unknown'}
    try:
        created, tx = await find_creation(item,reads,archive,pages)
        result.update(dev=created.creator,create_decode='pass',create=created.dict(),
                      create_buy=creation_buy(created,tx,config))
        result['hotlist']=await admission(created,reads,archive,config,pages)
        statuses=[result['hotlist']['status'],result['create_buy']['status']]
        result['candidate']='blocked' if 'blocked' in statuses else 'pass' if all(v=='pass' for v in statuses) else 'unknown'
    except RpcError:
        raise  # Retry rate limits and indexing/network errors durably.
    except Exception as exc:
        result['reason']=str(exc) if isinstance(exc,(Unknown,HistoryPending)) else type(exc).__name__
    result['execution']='not-simulated-not-an-order'
    result['required_wallets']=config.n
    result['unchecked_execution_gates']=['other-hotlist-wallet-votes','historical-market-cap','historical-dev-holdings',
                                       'historical-fees-tax-liquidity','live-feed-latency','simulation-and-inclusion']
    return result


def export(archive):
    results=[json.loads(r['body']) for r in archive.rows('','SELECT body FROM results ORDER BY event')]
    coverage=archive.rows('','SELECT time,kind,detail FROM coverage ORDER BY id')
    pending=archive.rows('',"SELECT state,count(*) n FROM queue GROUP BY state")
    report={'start':archive.start,'end':archive.end,'as_of':time.time(),'config':archive.setting('config'),
            'graduated_tokens':len({r['mint'] for r in results}),
            'candidate_counts':{s:sum(r['candidate']==s for r in results) for s in ('pass','blocked','unknown')},
            'queue':pending,'coverage':coverage,'tokens':results,
            'definition':'pass = original create decoded + dev funding/activity eligible + supported same-tx dev buy within static SOL amount gates; NOT an executable order',
            'limits':['No full historical replay or guarantee of all network graduations. Websocket gaps are explicitly recorded; no global history backfill.',
                      'A Create alone is not a buy signal. Wallet threshold, dynamic risk checks and execution are separate.',
                      'Historical wallet state and closed USDC accounts may be unavailable. Unknown is not rejected.',
                      'Current finalized account data verifies launchpad origin; original dev comes only from the original Create transaction.']}
    def write(name,text):
        target=archive.directory/name; tmp=target.with_suffix(target.suffix+'.tmp')
        tmp.write_text(text,encoding='utf-8');tmp.replace(target)
    write('report.json',json.dumps(report,ensure_ascii=False,indent=2))
    import io
    out=io.StringIO(newline=''); writer=csv.writer(out)
    writer.writerow(['launchpad','mint','dev','candidate','hotlist','hotlist_reason','create_decode','create_buy',
                     'create_buy_reason','create_tx','graduation_tx','reason'])
    for r in results:
        writer.writerow([r['launchpad'],r['mint'],r['dev'],r['candidate'],r.get('hotlist',{}).get('status','unknown'),
                         r.get('hotlist',{}).get('reason',''),r['create_decode'],r.get('create_buy',{}).get('status','unknown'),
                         r.get('create_buy',{}).get('reason',''),r.get('create',{}).get('signature',''),r['signature'],r.get('reason','')])
    write('report.csv','\ufeff'+out.getvalue())
    return report


async def websocket(config, archive, deadline):
    retry=1
    while time.time()<deadline:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(config.ws,heartbeat=20,max_msg_size=4*1024**2,
                                               timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    subscriptions={}; waiting={i+1:p for i,p in enumerate(PROGRAMS)}
                    for i,p in waiting.items():
                        await ws.send_json({'jsonrpc':'2.0','id':i,'method':'logsSubscribe',
                                            'params':[{'mentions':[p]},{'commitment':'finalized'}]})
                    while time.time()<deadline:
                        try: msg=await asyncio.wait_for(ws.receive(),timeout=min(5,max(.01,deadline-time.time())))
                        except asyncio.TimeoutError: continue
                        if msg.type!=aiohttp.WSMsgType.TEXT: raise Unknown('websocket-closed')
                        body=json.loads(msg.data)
                        if body.get('error'): raise Unknown('websocket-subscription-rejected')
                        if body.get('id') in waiting:
                            program=waiting.pop(body['id']);subscriptions[body['result']]=program
                            archive.coverage('subscribed',program)
                            retry=1
                            continue
                        params=body.get('params',{}); program=subscriptions.get(params.get('subscription'))
                        value=params.get('result',{}).get('value',{})
                        if not program or value.get('err') is not None or not value.get('signature'): continue
                        for kind,pool in log_hints(program,value.get('logs')):
                            if kind=='migration': archive.enqueue(value['signature'],params['result']['context']['slot'])
                            else:
                                with archive.db() as c:
                                    c.execute('INSERT OR IGNORE INTO hints VALUES (?,?)',(pool,value['signature']))
        except asyncio.CancelledError: raise
        except Exception as exc:
            archive.coverage('websocket-gap',type(exc).__name__)
            log.warning('Websocket gap; no global replay. type=%s retry_s=%s',type(exc).__name__,retry)
            await asyncio.sleep(min(retry,max(0,deadline-time.time())))
            retry=min(60,retry*2)


async def worker(config, archive, reads, pages, deadline):
    while time.time()<deadline:
        pending=archive.rows('',"SELECT * FROM queue WHERE state='pending' AND due<=? ORDER BY seen LIMIT 1",(time.time(),))
        if not pending:
            await asyncio.sleep(.5);continue
        row=pending[0];reads.reset()
        try:
            tx=Tx(await reads.transaction(row['signature']))
            if archive.start<=tx.time and (archive.end is None or tx.time<=archive.end):
                for item in migration_instructions(tx):
                    snapshot=await reads.call('getAccountInfo',[item['pool'],{'encoding':'base64','commitment':'finalized','minContextSlot':item['slot']}])
                    if verify_migration(item,snapshot):
                        archive.save_result(await analyse(item,reads,archive,config,pages))
            with archive.db() as c: c.execute("UPDATE queue SET state='done' WHERE signature=?",(row['signature'],))
        except asyncio.CancelledError: raise
        except Exception as exc:
            with archive.db() as c:
                c.execute("UPDATE queue SET attempts=attempts+1,due=?,reason=?,state=? WHERE signature=?",
                          (time.time()+60,type(exc).__name__,'unknown' if row['attempts']>=4 else 'pending',row['signature']))
            archive.coverage('migration-analysis-deferred',type(exc).__name__)


def snapshot_config(config):
    values = {name:str(getattr(config,name)) for name in (
        'min_funding','max_funding','min_funding_usdc','max_funding_usdc','hotlist_ttl','n','window',
        'ignore_observed_buy','min_observed_buy','max_observed_buy','min_observed_buy_usd_micros','max_observed_buy_usd_micros',
        'ignore_observed_buy_usd_micros','max_dev_holding_tokens','max_market_cap_usd_micros')}
    values.update(cex=config.cex,source_programs=list(config.source_programs),privacy_pools=list(config.privacy_pools))
    return values


async def watch(args, archive, config):
    if archive.setting('config') not in (None,snapshot_config(config)):
        raise ValueError('research-config-changed-use-a-new-output-directory')
    archive.setting('config',snapshot_config(config))
    deadline=archive.end+args.grace_seconds if archive.end is not None else math.inf
    if time.time()>=deadline:
        export(archive);return
    archive.coverage('started-or-resumed','Historical signalling/actual orders are not replayed.')
    async with aiohttp.ClientSession() as session:
        rpc=Rpc(session,config.rpc,Priority(),timeout=15,source='graduation-research')
        reads=Reads(rpc,archive,args.rps,args.max_rpc_per_token)
        if await reads.call('getGenesisHash',[])!='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d':
            raise ValueError('mainnet-required')
        stream_end=min(deadline,archive.end+60) if archive.end is not None else math.inf
        tasks=[asyncio.create_task(websocket(config,archive,stream_end)),
               asyncio.create_task(worker(config,archive,reads,args.pages,deadline))]
        try:
            while time.time()<deadline:
                if (archive.directory/'STOP').exists(): break
                report=export(archive)
                log.info('Graduates=%s candidates=%s queue=%s',report['graduated_tokens'],report['candidate_counts'],report['queue'])
                for task in tasks:
                    if task.done(): task.result()
                if time.time()>=stream_end and not any(r['state']=='pending' for r in report['queue']): break
                # Refresh reports every 30 seconds, but honour manual stops
                # within one second without cancelling a SQLite write midway.
                for _ in range(30):
                    if (archive.directory/'STOP').exists() or time.time()>=deadline: break
                    await asyncio.sleep(min(1,max(0,deadline-time.time())))
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            reason=('manual-stop' if (archive.directory/'STOP').exists() else
                    'window-complete' if time.time()>=stream_end else 'interrupted')
            archive.coverage('stopped',reason)
            export(archive)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['watch','report','stop'])
    parser.add_argument('--output',type=Path,default=ROOT/'data/graduation-research')
    parser.add_argument('--hours',type=float,default=None,help='optional fixed duration; omit to collect until manually stopped')
    parser.add_argument('--rps',type=float,default=.5,help='additional public HTTP calls/second, maximum 2')
    parser.add_argument('--pages',type=int,default=3,help='bounded history pages per address')
    parser.add_argument('--max-rpc-per-token',type=int,default=120)
    parser.add_argument('--grace-seconds',type=int,default=3600,help='maximum analysis drain after the window; reports remain readable meanwhile')
    args=parser.parse_args(argv)
    if args.hours is not None and (not math.isfinite(args.hours) or not 0<args.hours<=168):
        parser.error('hours must be >0 and <=168')
    if not math.isfinite(args.rps) or not 0<args.rps<=2: parser.error('rps must be >0 and <=2')
    if not 1<=args.pages<=20 or not 10<=args.max_rpc_per_token<=1000: parser.error('invalid query bounds')
    if not 0<=args.grace_seconds<=3600: parser.error('invalid grace period')
    if args.command in ('report','stop') and not (args.output/'research.sqlite3').is_file(): parser.error('research archive does not exist')
    if args.command=='stop':
        (args.output/'STOP').touch()
        print('Stop requested; the watcher will save its report and exit. Pending work is retained.')
        return
    if args.command=='report':
        # The watcher replaces the JSON file atomically. Reporting never opens
        # its SQLite database for writing or contends for the live writer lock.
        path=args.output/'report.json'
        if not path.is_file(): parser.error('first report is not available yet')
        result=json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps({k:result[k] for k in ('start','end','graduated_tokens','candidate_counts','queue')},indent=2))
        print(str(args.output.resolve()/'report.csv'))
        return
    lock=Instance(args.output)
    try:
        archive=Archive(args.output,args.hours)
        if args.command=='watch':
            if args.hours is None and archive.end is not None:
                archive.setting('end',None)
                archive.end=None
                archive.coverage('duration-changed','continuous-until-manual-stop')
            (archive.directory/'STOP').unlink(missing_ok=True)
            import os
            config=load(env={**os.environ,'DRY_RUN':'true','SOL_WALLET_ADDRESS':''})
            logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
            asyncio.run(watch(args,archive,config))
        result=export(archive)
        print(json.dumps({k:result[k] for k in ('start','end','graduated_tokens','candidate_counts','queue')},indent=2))
        print(str(args.output.resolve()/'report.csv'))
    except KeyboardInterrupt: pass
    except Exception as exc: parser.exit(1,'Research stopped: '+type(exc).__name__+'\n')
    finally: lock.close()


if __name__=='__main__':main()
