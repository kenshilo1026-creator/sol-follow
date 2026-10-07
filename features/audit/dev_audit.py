"""Read-only local audit. Run: python -m features.audit.dev_audit --help"""
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import json
import math
from pathlib import Path
import sqlite3
import time

ROOT=Path(__file__).resolve().parents[2]
REASONS={
 'failed-transaction':'鏈上交易失敗，不作入場或跟買',
 'processed-not-live-or-invalid':'processed 訊號不是即時或已失效',
 'chain-time-outside-coverage':'交易時間超出補查範圍',
 'signed-activity-within-30d':'入金前 30 天內有錢包簽署的交易',
 'wallet-history-check-incomplete':'歷史檢查尚未完成，等待重試',
 'funding-below-minimum':'CEX 入金低於下限','funding-above-maximum':'CEX 入金超過上限',
 'wallet-off-curve':'地址不是一般可簽署錢包','wallet-account-missing':'RPC 未取得錢包帳戶',
 'wallet-not-system-account':'帳戶不是一般 System 錢包','qualified':'已通過入場檢查',
 'funding-expired':'入金已超過有效期','hotlist-consumed':'錢包已移除，舊入金不能重新入場',
 'funding-already-recorded':'同一入金已處理','source-buy-below-minimum':'買額低於門檻，移除 hotlist',
 'source-sol-budget-over-limit':'目標 SOL 買入預算超過上限',
 'source-token-budget-over-sol-limit':'非 SOL 買入預算換算後超過上限',
 'price-cache-miss':'價格快取不足或過期，無法判定',
 'market-cap-price-unavailable':'美元價格不可用，無法判定',
 'market-cap-not-permitted':'市值未通過或資料不足，詳見市值紀錄',
 'not-eligible-hotlist':'當時沒有符合入金時間、slot、有效期及移除條件的資格',
 'processed-proof-not-pending':'沒有可用的 processed 即時訊號',
 'source-proof-unusable':'來源交易證據失效或過期',
 'confirmed-recovery-not-live':'confirmed 補查只作恢復，不觸發遲到跟買',
 'signal-too-old-or-before-start':'買入訊號過期或早於本次啟動',
 'duplicate-event':'同一買入事件已處理','not-buy':'這個事件不是買入',
 'wallet-threshold-not-reached':'有效錢包數尚未達 N（可能已賣出、過期或證據失效）',
 'order-dedup-or-cap-changed':'已有訂單／去重紀錄，或市值資格已改變',
 'order-reserved':'已達 N 並建立跟買訂單',
 'unsupported-route-or-not-a-buy':'未解析成支援的買入，不能斷言這是有效買入',
 'unsupported-non-sol-route':'偵測到不支援的非 SOL 買入路徑',
}


def stamp(value):
    return datetime.fromtimestamp(value,timezone(timedelta(hours=8))).isoformat(timespec='seconds')


@contextmanager
def connect(path):
    if not path.is_file():
        yield None
        return
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-2048')
        deadline=time.monotonic()+5
        c.set_progress_handler(lambda:time.monotonic()>deadline,10000)
        yield c
    finally:c.close()


def table(c,name):
    return c is not None and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def rows(c,name,where='1=1',params=(),limit=200,order='rowid DESC',columns='*'):
    if not table(c,name):return []
    return [dict(r) for r in c.execute(f'SELECT {columns} FROM {name} WHERE {where} ORDER BY {order} LIMIT ?',(*params,limit))]


def decode_details(items):
    for row in items:
        if isinstance(row.get('detail'),str):
            try:row['detail']=json.loads(row['detail'])
            except ValueError:row['detail']={'unavailable':'invalid-or-truncated-legacy-json'}
        if row.get('time'):row['time_hk']=stamp(row['time'])
        if row.get('reason'):row['explanation']=REASONS.get(row['reason'],row['reason'])
    return items


def category(row):
    if row['mode']!='live':return 'dry-run'
    if row['state']=='finalized':return 'finalized-buy'
    if row['state']=='confirmed':return 'confirmed-buy-not-finalized'
    if row['state'] in ('failed','expired'):return 'failed'
    return 'pending-or-unknown'


def orders(c,since,limit,wallet=None,all_states=False):
    where="side='buy' AND created>=?";params=[since]
    if not all_states:where+=" AND mode='live' AND state IN ('confirmed','finalized')"
    if wallet:
        if not table(c,'order_wallets'):return []
        where+=' AND id IN (SELECT order_id FROM order_wallets WHERE wallet=?)';params.append(wallet)
    found=rows(c,'orders',where,params,limit,'created DESC',
               'id,mode,mint,pool,state,amount,min_out,quoted_out,signature,created,updated,reason')
    for row in found:
        row['category']=category(row);row['created_hk']=stamp(row['created'])
        # Source wallets are saved with the reservation and survive vote cleanup.
        row['sources']=rows(c,'order_wallets','order_id=?',(row['id'],),10000)
        row['source_mapping']='recorded-at-reservation' if row['sources'] else 'unavailable-legacy-or-no-recorded-sources'
    return found


def report(data,command,hours=24,limit=200,wallet=None,all_states=False,now=None):
    now=time.time() if now is None else now;since=now-hours*3600;data=Path(data)
    result={'command':command,'hours':hours,'since_hk':stamp(since),'as_of_hk':stamp(now),
            'data':str(data.resolve()),'limit_per_section':limit,
            'notes':['本地資料缺失、未接收到事件或已清理，不等於該事件沒有發生。',
                     '詳細原因從新版服務啟動後開始記錄；舊訂單可能缺少來源錢包。',
                     '各資料庫分開讀取，運行中狀態可能在查詢期間更新。'],'missing_databases':[]}
    for name in ('funding','trading','audit'):
        if not (data/(name+'.sqlite3')).is_file():result['missing_databases'].append(name)
    with connect(data/'trading.sqlite3') as c:
        result['orders']=orders(c,since,limit,wallet,all_states or command=='wallet')
    result['at_limit_sections']=[]
    if command=='wallet':
        result['wallet']=wallet
        with connect(data/'funding.sqlite3') as c:
            result['hotlist_current']=rows(c,'hotlist','wallet=?',(wallet,),1)
            result['hotlist_active_now']=bool(result['hotlist_current'] and result['hotlist_current'][0]['expires']>now)
            result['removal']=rows(c,'hotlist_removals','wallet=?',(wallet,),1)
            result['funding']=rows(c,'funding','wallet=? AND time>=?',(wallet,since),limit)
        with connect(data/'audit.sqlite3') as c:
            result['decisions']=decode_details(rows(c,'decisions','wallet=? AND time>=?',(wallet,since),limit,'time DESC,id DESC'))
            result['audit_coverage']={'oldest_retained':rows(c,'decisions',limit=1,order='time ASC',columns='time'),
                                      'newest_retained':rows(c,'decisions',limit=1,order='time DESC',columns='time')}
            # Older notices may explain execution failures; never select raw signed orders.
            result['legacy_notices']=decode_details(rows(c,'audit',
                "time>=? AND CASE WHEN json_valid(detail) THEN COALESCE(json_extract(detail,'$.wallet'),json_extract(detail,'$.source_wallet'))=? ELSE 0 END",
                (since,wallet),limit,'time DESC'))
        sigs=list(dict.fromkeys(r['signature'] for r in result['funding']+result['decisions'] if r.get('signature')))
        events=list(dict.fromkeys(r['event'] for r in result['funding']+result['decisions'] if r.get('event')))
        with connect(data/'funding.sqlite3') as c:
            result['source_proofs']=rows(c,'processed_signals','signature IN ('+','.join('?'*len(sigs))+')',sigs,limit) if sigs else []
            result['jobs']=rows(c,'jobs','signature IN ('+','.join('?'*len(sigs))+')',sigs,limit) if sigs else []
            result['activity_checks']=decode_details(rows(c,'funding_activity_checks','event IN ('+','.join('?'*len(events))+')',events,limit)) if events else []
        mints=list(dict.fromkeys(r['mint'] for r in result['orders']+result['decisions'] if r.get('mint')))
        with connect(data/'trading.sqlite3') as c:
            result['market_caps']=decode_details(rows(c,'token_entry_caps','mint IN ('+','.join('?'*len(mints))+')',mints,limit)) if mints else []
        ids=[r['id'] for r in result['orders']]
        with connect(data/'audit.sqlite3') as c:
            result['execution_notices']=decode_details(rows(c,'audit',
                "CASE WHEN json_valid(detail) THEN json_extract(detail,'$.order') IN ("+','.join('?'*len(ids))+") ELSE 0 END",
                ids,limit,'time DESC')) if ids else []
        if not result['decisions']:result['notes'].append('期間內沒有此錢包的詳細判定，無法據此斷言被哪條規則拒絕。')
    else:
        result['notes'].append('時間範圍按跟買訂單建立時間；預設只列 live confirmed/finalized。--all 另列失敗、待確認與 dry-run。')
    result['at_limit_sections']=[k for k,v in result.items() if isinstance(v,list) and len(v)>=limit and k not in ('notes','at_limit_sections','missing_databases')]
    return result


def display(result):
    print(f"{result['command']} | {result['since_hk']} 至 {result['as_of_hk']}")
    if result.get('wallet'):
        print('錢包:',result['wallet'],'目前 hotlist:',result['hotlist_active_now'])
        for row in reversed(result['decisions']):
            print(row['time_hk'],row['stage'],row['outcome'],row['explanation'],
                  'token='+row['mint'],'tx='+row['signature'])
            if row['detail']:print('  ',json.dumps(row['detail'],ensure_ascii=False))
        for key in ('removal','funding','jobs','source_proofs','activity_checks','market_caps','legacy_notices','execution_notices'):
            if result[key]:print(key+':',json.dumps(result[key],ensure_ascii=False))
    print('跟買訂單:',len(result['orders']))
    for row in result['orders']:
        print(row['created_hk'],row['category'],row['state'],'token='+row['mint'],
              'order='+row['id'],'tx='+str(row['signature'] or ''),'reason='+row['reason'])
        print('  來源錢包:',','.join(dict.fromkeys(r['wallet'] for r in row['sources'])) or '無歷史來源紀錄')
    for note in result['notes']:print('註:',note)
    if result['missing_databases']:print('缺少資料庫:',','.join(result['missing_databases']))
    if result['at_limit_sections']:print('已達筆數上限，可能有更多紀錄:',','.join(result['at_limit_sections']))


def main(argv=None):
    from chain_common.primitives import pubkey
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name,aliases in [('wallet',['dev']),('followed',['buys'])]:
        p=sub.add_parser(name,aliases=aliases)
        if name=='wallet':p.add_argument('wallet',type=lambda s:str(pubkey(s)))
        else:p.add_argument('--all',action='store_true',help='include dry runs, failures and pending orders')
        p.add_argument('--hours',type=float,default=24)
        p.add_argument('--limit',type=int,default=200)
        p.add_argument('--data',type=Path,default=ROOT/'data')
        p.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    if not math.isfinite(args.hours) or not 0<args.hours<=8760:parser.error('--hours must be > 0 and <= 8760')
    if not 1<=args.limit<=10000:parser.error('--limit must be between 1 and 10000')
    command='wallet' if args.command in ('wallet','dev') else 'followed'
    try:result=report(args.data,command,args.hours,args.limit,getattr(args,'wallet',None),getattr(args,'all',False))
    except sqlite3.Error as exc:parser.exit(1,'本地資料庫讀取失敗或查詢超時: '+type(exc).__name__+'\n')
    if args.json:print(json.dumps(result,ensure_ascii=False,indent=2))
    else:display(result)


if __name__=='__main__':main()
