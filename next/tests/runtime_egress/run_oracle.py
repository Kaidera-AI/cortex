"""Effect guard around the EXISTING test_receipts runner; no new classifier.

Preload dependency modules before guards. Test bodies may use MockTransport and
AF_UNIX asyncio wakeups, never IP sockets, resolver DNS, PG or child processes.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sys
from unittest.mock import patch

NEXT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(NEXT/'tests'), str(NEXT/'tests/runtime')]
# Imports are not deciding tests and any failure here is a runner error.
import asyncpg
import httpx
import jsonschema
import psycopg
import cortex_core.api.c11b2


def main():
    attempts = []
    real_socket = socket.socket
    def deny(name):
        def blocked(*args, **kwargs):
            attempts.append(name)
            raise RuntimeError('E0 forbidden real effect: '+name)
        return blocked
    class LocalSocket(real_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family in (socket.AF_INET, socket.AF_INET6):
                deny('IP socket')()
            super().__init__(family, *args, **kwargs)
        connect = deny('socket connect')
        connect_ex = deny('socket connect_ex')
        bind = deny('socket bind')
    status = 2
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket, 'socket', LocalSocket))
        stack.enter_context(patch.object(socket, 'getaddrinfo', deny('DNS')))
        for target in ('subprocess.Popen','os.system','os.fork','os.posix_spawn',
                       'asyncpg.connect','asyncpg.create_pool','psycopg.connect',
                       'psycopg.AsyncConnection.connect','psycopg.Connection.connect'):
            stack.enter_context(patch(target, side_effect=deny(target)))
        sys.argv = [str(NEXT/'tests/test_receipts.py'), str(Path(__file__).parent)]
        try:
            runpy.run_path(sys.argv[0], run_name='__main__')
        except SystemExit as exc:
            status = exc.code
    loaded = {}
    for module in tuple(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename and Path(filename).is_relative_to(NEXT/'src') and Path(filename).is_file():
            loaded[str(Path(filename).relative_to(NEXT))] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    print('CORTEX_EFFECT_RESULT='+json.dumps({'attempts':attempts, 'loaded_sources':loaded,
        'python':sys.version, 'dependencies':{m.__name__:getattr(m,'__version__','unknown')
        for m in (asyncpg,httpx,psycopg)}}), flush=True)
    return status if not attempts else 2


if __name__ == '__main__':
    raise SystemExit(main())
