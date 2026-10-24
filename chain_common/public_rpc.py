"""One credential-free 429 notification entry point for Python and SDK RPCs."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re

_sink=ContextVar('public_rpc_notice_sink',default=None)
_defer_429=ContextVar('defer_rpc_429_alerts',default=False)


@contextmanager
def rpc_notices(notices):
    token=_sink.set(notices)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def defer_429_alerts(enabled=True):
    """Keep per-provider evidence local while this task tries RPC recovery."""
    if not enabled:
        yield
        return
    token=_defer_429.set(True)
    try:
        yield
    finally:
        _defer_429.reset(token)


def report_429(source,method,transport):
    # Accept only labels, never endpoint URLs, request parameters or error text.
    source=source if source in ('python','background','node-sdk','public-websocket','alchemy-history','alchemy-websocket') else 'public-rpc'
    method=method if isinstance(method,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',method) else 'unknown'
    transport=transport if transport in ('HTTP','JSON-RPC','WS-handshake','WS-RPC') else 'unknown'
    detail={'source':source,'method':method,'transport':transport,'status':429}
    kind='Alchemy RPC 429' if source in ('alchemy-history','alchemy-websocket') else 'public RPC 429'
    sink=_sink.get()
    if sink is None:
        logging.getLogger('sol-follow').warning('[SOL] %s %s',kind,detail)
        return
    # Record each actual 429, but RPC recovery decides whether to alert.
    sink.emit(kind,detail,alert=not _defer_429.get())


def report_funding_unavailable(method):
    _report_unavailable('funding RPC unavailable','funding-rpc-unavailable',method)


def report_confirmation_unavailable(method):
    _report_unavailable('confirmation RPC unavailable','confirmation-rpc-unavailable',method)


def _report_unavailable(kind,key,method):
    method=method if isinstance(method,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',method) else 'unknown'
    detail={'method':method,'reason':'public-and-alchemy-unavailable'}
    sink=_sink.get()
    if sink is None:
        logging.getLogger('sol-follow').warning('[SOL] %s %s',kind,detail)
        return
    # Cooling providers can fail many queued jobs without another network call.
    sink.emit(kind,detail,alert=True,key=key+':'+method,interval=60)


def sdk_event(row):
    if isinstance(row,dict) and row.get('event')=='public-rpc-429':
        report_429('node-sdk',row.get('method'),row.get('transport'))
        return True
    return False
