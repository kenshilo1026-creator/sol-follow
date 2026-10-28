"""Credential-free database diagnostics; never echo arbitrary exception text."""
import sqlite3
from pathlib import Path


REASONS = {
    sqlite3.SQLITE_BUSY: '資料庫忙碌，其他操作正在持有鎖',
    sqlite3.SQLITE_LOCKED: '資料表或連線被其他操作鎖定',
    sqlite3.SQLITE_INTERRUPT: 'SQL 查詢被中斷（例如清理超時回呼）',
    sqlite3.SQLITE_READONLY: '資料庫無法寫入（唯讀或權限限制）',
    sqlite3.SQLITE_FULL: '資料庫或磁碟空間不足',
    sqlite3.SQLITE_CANTOPEN: '無法開啟資料庫檔案',
    sqlite3.SQLITE_IOERR: '資料庫磁碟讀寫錯誤',
    sqlite3.SQLITE_CORRUPT: 'SQLite 偵測到資料庫結構損壞',
    sqlite3.SQLITE_NOTADB: '檔案不是有效的 SQLite 資料庫',
}


def detail(exc):
    result={'type':type(exc).__name__}
    if isinstance(exc,sqlite3.Error):
        code=getattr(exc,'sqlite_errorcode',None)
        if type(code) is int:
            result['sqlite_code']=code
            result['reason']=REASONS.get(code & 255,'SQLite 操作失敗，需按錯誤碼定位')
        else:
            result['reason']='SQLite 操作失敗，未提供錯誤碼'
        name=getattr(exc,'sqlite_errorname',None)
        if isinstance(name,str) and name.startswith('SQLITE_') and name.replace('_','').isalnum():
            result['sqlite_name']=name
    tb=exc.__traceback__
    if tb:
        while tb.tb_next:tb=tb.tb_next
        result['location']=f'{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}'
    return result


def failures(exc):
    if isinstance(exc,BaseExceptionGroup):
        return [item for child in exc.exceptions for item in failures(child)][:8]
    return [detail(exc)]
