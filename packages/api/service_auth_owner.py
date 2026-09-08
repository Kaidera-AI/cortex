"""API-owned, bounded HTTP/1.1 over an owner-private pathname Unix socket.

No TCP listener, database connection, automatic startup or registration SQL.
The caller supplies the existing store, event loop and canonical resolver.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
from uuid import UUID

import h11
import service_auth as auth

PROTOCOL = 'cortex.owner.v1'
MAX_HEADER = 8192
MAX_BODY = 16384
MAX_RESPONSE = 32768
TIMEOUT = 5
SOCKET_NAME = 'owner.sock'


class OwnerError(Exception):
    """Sanitized transport failure; never carries request or driver diagnostics."""


def peer_uid(sock):
    if sys.platform.startswith('linux'):
        return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                 struct.calcsize('3i')))[1]
    if sys.platform == 'darwin':
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        if getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    raise OwnerError('owner peer identity unavailable')


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OwnerError('owner directory must be an absolute private path')
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise OwnerError('owner paths must not follow symlinks')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise OwnerError('owner directory must be owned and mode 0700')
    return path


def private_socket(path):
    path = Path(path)
    private_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise OwnerError('owner socket must be owned and mode 0600')
    return path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise OwnerError('duplicate owner request fields')
        result[key] = value
    return result


def decode(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(OwnerError('invalid JSON constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise OwnerError('invalid owner JSON') from None


def encode(value):
    def convert(item):
        if isinstance(item, UUID): return str(item)
        if isinstance(item, datetime): return item.isoformat()
        if isinstance(item, frozenset): return sorted(item)
        raise TypeError('unsupported owner response type')
    return json.dumps(value, default=convert, separators=(',', ':'), allow_nan=False).encode()


def validate_request(value):
    if not isinstance(value, dict) or value.get('protocol') != PROTOCOL:
        raise OwnerError('unsupported owner protocol')
    operation = value.get('operation')
    keys = {'protocol', 'operation', 'instance_id'}
    if operation == 'setup-grant': keys |= {'purpose'}
    elif operation == 'consume-setup':
        keys |= {'purpose', 'setup_token', 'identity', 'installation_id', 'scopes', 'revoke_existing'}
    elif operation != 'identity': raise OwnerError('unsupported owner operation')
    if set(value) != keys:
        raise OwnerError('invalid owner fields')
    instance = value['instance_id']
    if instance is not None or operation != 'identity':
        try:
            if not isinstance(instance, str) or str(UUID(instance)) != instance: raise ValueError
        except ValueError:
            raise OwnerError('invalid owner instance') from None
    if operation != 'identity' and value['purpose'] not in {'bootstrap', 'recovery'}:
        raise OwnerError('invalid owner purpose')
    if operation == 'consume-setup':
        identity = value['identity']
        if (not isinstance(identity, dict) or set(identity) != {'project_key', 'agent_name'}
                or any(not isinstance(v, str) or not 1 <= len(v) <= 200 or any(ord(c) < 32 for c in v)
                       for v in identity.values())
                or not isinstance(value['setup_token'], str) or len(value['setup_token']) > 128
                or not isinstance(value['installation_id'], str) or not 1 <= len(value['installation_id']) <= 200
                or not isinstance(value['scopes'], list) or not value['scopes']
                or any(not isinstance(s, str) or s not in auth.SCOPES for s in value['scopes'])
                or len(set(value['scopes'])) != len(value['scopes'])
                or type(value['revoke_existing']) is not bool):
            raise OwnerError('invalid owner enrollment')
    return value


class OwnerServer:
    def __init__(self, directory, store, resolve_identity, *, timeout=TIMEOUT, max_connections=8):
        self.directory = Path(directory)
        self.socket_path = self.directory / SOCKET_NAME
        self.lock_path = self.directory / 'owner.lock'
        self.store, self.resolve_identity = store, resolve_identity
        if not 0 < timeout <= 30 or not 1 <= max_connections <= 32:
            raise OwnerError('invalid owner resource bounds')
        self.timeout, self.max_connections = timeout, max_connections
        self.tasks, self.server, self.owned = set(), None, {}

    async def start(self):
        try:
            if (not self.directory.is_absolute() or '..' in self.directory.parts
                    or any(p.is_symlink() for p in [*self.directory.parents, self.directory])):
                raise OwnerError('owner paths must not follow symlinks')
            if not self.directory.exists() and not self.directory.is_symlink():
                # Parent must already exist; never invent a broad directory tree.
                self.directory.mkdir(mode=0o700)
            private_directory(self.directory)
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                self.owned[self.lock_path] = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
            if self.socket_path.exists() or self.socket_path.is_symlink():
                raise OwnerError('existing owner endpoint requires deliberate recovery')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(self.socket_path))
                info = self.socket_path.lstat()
                self.owned[self.socket_path] = (info.st_dev, info.st_ino)
                os.chmod(self.socket_path, 0o600, follow_symlinks=False)
                private_socket(self.socket_path)
                sock.setblocking(False)
                self.server = await asyncio.start_unix_server(self._accept, sock=sock,
                    limit=MAX_HEADER + MAX_BODY, cleanup_socket=False)
            except BaseException:
                sock.close()
                raise
            return self
        except (OSError, ValueError):
            await self.close()
            raise OwnerError('owner listener path unavailable') from None
        except BaseException:
            await self.close()
            raise

    def _accept(self, reader, writer):
        if len(self.tasks) >= self.max_connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, reader, writer):
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADER)
        try:
            private_socket(self.socket_path)
            if peer_uid(writer.get_extra_info('socket')) != os.getuid():
                raise OwnerError('owner peer refused')
            await asyncio.wait_for(self._exchange(reader, writer, conn), self.timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            pass  # The whole-request deadline is final; do not extend it to write.
        except Exception:
            # No request, exception, token or driver diagnostics reach logs.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._respond(writer, conn, 503, {'error': 'unavailable'}), 0.2)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 0.2)

    async def _exchange(self, reader, writer, conn):
        wire, body, request = bytearray(), bytearray(), None
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                chunk = await reader.read(4096)
                wire.extend(chunk)
                if len(wire) > MAX_HEADER + MAX_BODY:
                    raise OwnerError('owner request too large')
                conn.receive_data(chunk)
            elif isinstance(event, h11.Request):
                if request is not None: raise OwnerError('multiple owner requests')
                request = event
                # h11 parses framing. Comparing its serialized header prevents
                # accepted normalization (duplicate/equal-comma lengths, folded
                # lines) from weakening this deliberately narrow wire contract.
                header = h11.Connection(h11.CLIENT).send(event)
                if len(header) > MAX_HEADER or not wire.startswith(header):
                    raise OwnerError('noncanonical owner headers')
                headers = dict(event.headers)
                if (len(headers) != len(event.headers)
                        or set(headers) != {b'host', b'content-type', b'content-length'}
                        or headers[b'host'] != b'cortex-owner'
                        or headers[b'content-type'] != b'application/json'
                        or int(headers[b'content-length']) > MAX_BODY
                        or event.method != b'POST' or event.target != b'/v1/owner'
                        or event.http_version != b'1.1'):
                    raise OwnerError('unsupported owner HTTP request')
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > MAX_BODY: raise OwnerError('owner body too large')
            elif isinstance(event, h11.EndOfMessage):
                if request is None or conn.trailing_data[0]:
                    raise OwnerError('unexpected owner request data')
                value = validate_request(decode(body))
                instance = await self.store.instance_identity(authority=auth.LOCAL_OWNER)
                if value['instance_id'] is not None and value['instance_id'] != instance:
                    raise OwnerError('owner instance mismatch')
                operation = value['operation']
                if operation == 'identity':
                    result = {'instance_id': instance}
                elif operation == 'setup-grant':
                    result = asdict(await self.store.create_setup_grant(authority=auth.LOCAL_OWNER,
                        purpose=value['purpose'], expected_instance_id=value['instance_id']))
                else:
                    async def resolve(conn):
                        return await self.resolve_identity(conn, value['identity'])
                    result = asdict(await self.store.consume_setup_grant(value['setup_token'],
                        authority=auth.LOCAL_OWNER, resolve_identity=resolve,
                        installation_id=value['installation_id'], scopes=value['scopes'],
                        purpose=value['purpose'], revoke_existing=value['revoke_existing'],
                        expected_instance_id=value['instance_id']))
                await self._respond(writer, conn, 200, {'protocol': PROTOCOL, 'instance_id': instance, 'result': result})
                return
            else:
                raise OwnerError('incomplete owner request')

    async def _respond(self, writer, conn, status, value):
        data = encode(value)
        if len(data) > MAX_RESPONSE: raise OwnerError('owner response too large')
        writer.write(conn.send(h11.Response(status_code=status, headers=[
            ('Content-Type', 'application/json'), ('Content-Length', str(len(data))),
            ('Cache-Control', 'no-store'), ('Connection', 'close')])))
        writer.write(conn.send(h11.Data(data=data)))
        writer.write(conn.send(h11.EndOfMessage()))
        await writer.drain()

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = tuple(self.tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for path, inode in reversed(tuple(self.owned.items())):
            with contextlib.suppress(FileNotFoundError):
                info = path.lstat()
                if (info.st_dev, info.st_ino) == inode: path.unlink()
        self.owned.clear()

    async def __aenter__(self): return await self.start()

    async def __aexit__(self, *exc): await self.close()
