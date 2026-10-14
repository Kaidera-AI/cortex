"""One task-scoped privacy boundary for connection-owning driver work.

RC3 can use private_driver_logs unchanged around its connection operations.
Record creation precedes both direct handlers and root propagation; ancestor
logger filters alone cannot protect propagated records. Child asyncio tasks
inherit the scope, while unrelated tasks retain their original diagnostics.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from threading import Lock

_PRIVATE = ContextVar('cortex_private_driver_logs', default=False)
_INSTALL_LOCK = Lock()
_FIXED_CLOSE_MESSAGES = frozenset({
    'Core probe connection close failed', 'Core probe connection close timed out'})


def _install():
    with _INSTALL_LOCK:
        previous = logging.getLogRecordFactory()
        if getattr(previous, '_cortex_driver_privacy', False):
            return

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            if not _PRIVATE.get():
                return record
            fixed_close = (record.name == 'cortex_core.api.c11a'
                and isinstance(record.msg, str) and record.msg in _FIXED_CLOSE_MESSAGES
                and not record.args
                and not record.exc_info and not record.exc_text and not record.stack_info)
            if not fixed_close:
                # Never format or inspect exception values or connection arguments.
                record.msg = 'Core driver diagnostic redacted'
                record.args = ()
                record.exc_info = record.exc_text = record.stack_info = None
            return record

        factory._cortex_driver_privacy = True
        logging.setLogRecordFactory(factory)


@contextmanager
def private_driver_logs():
    """Install before I/O and protect all logging during owned driver work.

    The wrapper chains the current factory and installs once per configuration.
    Keep logging configuration stable during an operation; later operations
    reinstall if the application's configuration replaced the record factory.
    """
    _install()
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)
