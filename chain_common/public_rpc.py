"""One credential-free 429 notification entry point for Python and SDK RPCs."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re

_sink=ContextVar('public_rpc_notice_sink',default=None)


@contextmanager
def rpc_notices(notices):
    token=_sink.set(notices)
    try:
        yield
    finally:
        _sink.reset(token)


def report_429(source,method,transport):
    # Accept only labels, never endpoint URLs, request parameters or error text.
    source=source if source in ('python','background','node-sdk','public-websocket','alchemy-history') else 'public-rpc'
    method=method if isinstance(method,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',method) else 'unknown'
    transport=transport if transport in ('HTTP','JSON-RPC','WS-handshake','WS-RPC') else 'unknown'
    detail={'source':source,'method':method,'transport':transport,'status':429}
    kind='Alchemy RPC 429' if source=='alchemy-history' else 'public RPC 429'
    sink=_sink.get()
    if sink is None:
        logging.getLogger('sol-follow').warning('[SOL] %s %s',kind,detail)
        return
    # No deduplication: each actual provider 429 is a separate alert.
    sink.emit(kind,detail,alert=True)


def sdk_event(row):
    if isinstance(row,dict) and row.get('event')=='public-rpc-429':
        report_429('node-sdk',row.get('method'),row.get('transport'))
        return True
    return False
