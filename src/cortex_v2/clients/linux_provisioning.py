"""Finite Linux host process port. Private frames never enter argv or logs."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import native_prerequisite as custody
from .native_prerequisite import PrerequisiteRefusal, strict_json
from .provisioning import ProvisionRefusal, _response, validate_request


REFUSALS = frozenset({
    'cortex_health_unavailable', 'cortex_health_degraded', 'cortex_credential_refused',
    'cortex_credential_unavailable', 'cortex_provisioning_reissue_required',
    'cortex_provisioning_setup_required', 'cortex_provisioning_owner_required',
    'cortex_provisioning_conflict', 'cortex_descriptor_invalid',
    'cortex_descriptor_owner_mismatch', 'cortex_instance_mismatch',
    'cortex_release_unsupported', 'cortex_release_signature_invalid',
    'cortex_podman_denied', 'cortex_podman_unsupported', 'cortex_image_mismatch',
    'cortex_cgroup_delegation_unavailable',
})


def _local_engine_context() -> dict:
    """Bind the actual kos identity and physical local engine before execution."""
    try:
        uid = os.getuid()
        if platform.system() != 'Linux' or uid == 0 or os.geteuid() != uid:
            raise ValueError
        account = pwd.getpwuid(uid)
        if account.pw_name != 'kos':
            raise ValueError
        home, runtime, executable = Path(account.pw_dir), Path('/run/user/' + str(uid)), Path('/usr/bin/podman')
        identities = []
        for path, owner, directory in ((home, uid, True), (runtime, uid, True), (executable, 0, False)):
            custody.physical_path(path)
            info = path.lstat()
            if (info.st_uid != owner or info.st_mode & 0o022
                    or (directory and not stat.S_ISDIR(info.st_mode))
                    or (not directory and (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111))):
                raise ValueError
            identities.append(custody._file_identity(info))
        return {'executable': str(executable), 'uid': uid, 'identity': tuple(identities),
                'environment': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
                                'XDG_RUNTIME_DIR': str(runtime), 'LC_ALL': 'C'}}
    except (OSError, KeyError, ValueError, TypeError, PrerequisiteRefusal):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def _engine_json(command: list[str], *, environment: dict, deadline: float) -> dict:
    """Read-only process, bounded pipes and cleanup; raw diagnostics stay local."""
    try:
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic() or not isinstance(command, list)
                or not 1 <= len(command) <= 32 or not Path(command[0]).is_absolute()
                or any(not isinstance(arg, str) or '\x00' in arg or len(arg) > 4096 for arg in command)
                or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or '\x00' in k + v for k, v in environment.items())):
            raise ValueError
        absolute_deadline = min(deadline, time.monotonic() + 5)
        cleanup_budget = min(.1, (absolute_deadline - time.monotonic()) / 4)
        work_deadline = absolute_deadline - cleanup_budget
    except (OSError, ValueError, TypeError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    process = None
    complete = False
    output, errors = bytearray(), bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(environment), close_fds=True,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, 'output'), (process.stderr, 'error')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                for key, _ in selector.select(min(remaining, .05)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    buffer, limit = (output, 65536) if key.data == 'output' else (errors, 4096)
                    if len(buffer) + len(chunk) > limit:
                        raise ValueError
                    buffer.extend(chunk)
            complete = True
        if process.returncode != 0 or errors or time.monotonic() >= work_deadline:
            raise ValueError
        return strict_json(bytes(output))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, PrerequisiteRefusal):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    finally:
        if process is not None:
            if not complete:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=max(0, absolute_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def observe_linux_engine(*, cortex_policy: dict, kos_policy: dict,
                         deadline: float | None = None) -> dict:
    """Finite actual local reads, before any producer write; no host repair."""
    now = time.monotonic()
    if deadline is None:
        deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
        raise PrerequisiteRefusal('cortex_podman_unsupported')
    deadline = min(deadline, now + 60)
    context = _local_engine_context()

    def read(kind):
        if time.monotonic() >= deadline or _local_engine_context() != context:
            raise PrerequisiteRefusal('cortex_podman_unsupported')
        value = _engine_json([context['executable'], '--remote=false', kind, '--format=json'],
                             environment=context['environment'], deadline=deadline)
        if time.monotonic() >= deadline or _local_engine_context() != context:
            raise PrerequisiteRefusal('cortex_podman_unsupported')
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')

    try:
        delegation = custody.check_linux_delegation(read('info'))
        version = custody.parse_podman_report(read('version'), host_os='linux', mode='local',
            connection_name=None, expected_connection_name=None,
            cortex_policy=cortex_policy, kos_policy=kos_policy)
        return {'schema': 'cortex.linux-engine-readiness.v1', **version, 'delegation': delegation}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


class LoopbackTransport:
    """One bounded GET to the explicitly bound local API; never proxy or redirect."""

    def __init__(self, origin: str):
        if (not isinstance(origin, str)
                or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', origin)
                or not 1 <= int(origin.rsplit(':', 1)[1]) <= 65535):
            raise ProvisionRefusal('cortex_health_unavailable')
        self.origin = origin
        self.port = int(origin.rsplit(':', 1)[1])

    def get(self, url: str, *, headers: dict, timeout: float, max_bytes: int) -> tuple[int, bytes]:
        try:
            if (not isinstance(url, str) or len(url) > 4096 or '#' in url
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                    or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5
                    or type(max_bytes) is not int or not 1 <= max_bytes <= 65536
                    or not isinstance(headers, dict) or set(headers) != {'Authorization', 'X-Cortex-Scope'}
                    or not isinstance(headers['Authorization'], str)
                    or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', headers['Authorization'])
                    or not isinstance(headers['X-Cortex-Scope'], str)
                    or not custody.IDENTIFIER.fullmatch(headers['X-Cortex-Scope'])):
                raise ValueError
            parsed = urlsplit(url)
            if (parsed.scheme != 'http' or 'http://' + parsed.netloc != self.origin
                    or not parsed.path.startswith('/') or parsed.fragment):
                raise ValueError
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
        except (ValueError, TypeError):
            raise ProvisionRefusal('cortex_health_unavailable') from None

        deadline = time.monotonic() + timeout
        work_deadline = deadline - min(.05, timeout / 4)
        connection = response = owned_socket = timer = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
            connection.connect()
            owned_socket = connection.sock
            remaining = work_deadline - time.monotonic()
            if remaining <= 0:
                raise ProvisionRefusal('cortex_health_unavailable')
            owned_socket.settimeout(remaining)

            def interrupt():
                # Own the socket object, not a descriptor number which may be reused.
                try:
                    owned_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(remaining, interrupt)
            timer.daemon = True
            timer.start()
            connection.request('GET', path, headers=dict(headers))
            response = connection.getresponse()
            raw = response.read(max_bytes + 1)
            if (len(raw) > max_bytes or time.monotonic() >= work_deadline
                    or response.length not in (None, 0)):
                raise ProvisionRefusal('cortex_health_unavailable')
            return response.status, raw
        except (OSError, ValueError, http.client.HTTPException):
            raise ProvisionRefusal('cortex_health_unavailable') from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=max(0, min(.05, deadline - time.monotonic())))
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            if owned_socket is not None:
                owned_socket.close()


def _one_frame(raw: bytes, limit: int) -> dict:
    if (not raw.endswith(b'\n') or raw.count(b'\n') != 1
            or not raw.startswith(b'{') or raw[:-1].strip() != raw[:-1]):
        raise ProvisionRefusal('cortex_health_unavailable')
    try:
        value = strict_json(raw, limit=limit)
        # Escaped unpaired surrogates cannot become a valid UTF-8 response.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value
    except (PrerequisiteRefusal, ValueError, UnicodeError, TypeError, RecursionError):
        raise ProvisionRefusal('cortex_health_unavailable') from None


def private_json_command(command: list[str], frame: dict, *, timeout: float = 5) -> dict:
    """One private request/response with an absolute work and cleanup deadline.

    The caller binds the executable and owned object before calling this port.
    This function has no ambient credentials, engine defaults or retry.
    """
    try:
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or any(not isinstance(arg, str) or '\x00' in arg for arg in command)
                or not Path(command[0]).is_absolute()
                or not isinstance(frame, dict) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 5):
            raise ValueError
        request = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(request) > 65536: raise ValueError
        strict_json(request)
    except (ValueError, TypeError, UnicodeError, PrerequisiteRefusal, RecursionError):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None
    absolute_deadline = time.monotonic() + timeout
    cleanup_budget = min(.1, timeout / 4)
    work_deadline = absolute_deadline - cleanup_budget
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C',
                   'HOME': pwd.getpwuid(os.getuid()).pw_dir,
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    process = None
    streams_closed = False
    output, errors = bytearray(), bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, close_fds=True,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, name, event in ((process.stdin, 'input', selectors.EVENT_WRITE),
                                        (process.stdout, 'output', selectors.EVENT_READ),
                                        (process.stderr, 'error', selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, name)
            position = 0
            while selector.get_map() or process.poll() is None:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0: raise ProvisionRefusal('cortex_health_unavailable')
                for key, _ in selector.select(min(remaining, .05)):
                    stream = key.fileobj
                    if key.data == 'input':
                        try:
                            written = os.write(stream.fileno(), request[position:position + 4096])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(stream); stream.close()
                            continue
                        position += written
                        if position == len(request):
                            selector.unregister(stream); stream.close()
                    else:
                        try: chunk = os.read(stream.fileno(), 8192)
                        except BlockingIOError: continue
                        if not chunk:
                            selector.unregister(stream); stream.close()
                            continue
                        buffer, limit = (output, 65536) if key.data == 'output' else (errors, 4096)
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise ProvisionRefusal('cortex_health_unavailable')
            streams_closed = True
        if process.returncode == 0 and not errors:
            return _one_frame(bytes(output), 65536)
        if process.returncode == 2 and not output:
            refusal = _one_frame(bytes(errors), 4096)
            code = refusal.get('code')
            if isinstance(code, str) and code in REFUSALS:
                # Child messages, timestamps and all other fields are discarded.
                raise ProvisionRefusal(code)
        raise ProvisionRefusal('cortex_health_unavailable')
    except ProvisionRefusal:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise ProvisionRefusal('cortex_health_unavailable') from None
    finally:
        if process is not None:
            if not streams_closed:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: process.wait(timeout=max(0, absolute_deadline - time.monotonic()))
                except subprocess.TimeoutExpired: pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None: stream.close()


@contextmanager
def operation_lock(path: Path):
    """Nonblocking private inode lock; retain its inode between operations."""
    descriptor = None
    acquired = False
    try:
        try:
            parents = custody._parents(path)
            if path.exists() or path.is_symlink():
                custody.read_private_bytes(path, limit=0)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            opened = os.fstat(descriptor); custody._private_file(opened)
            if opened.st_size != 0 or custody._file_identity(path.lstat()) != custody._file_identity(opened):
                raise ProvisionRefusal('cortex_provisioning_conflict')
            custody._recheck_parents(parents)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        except (OSError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_conflict') from None
        # Body refusals belong to the operation and keep their original code.
        yield acquired
        if not acquired:
            return
        try:
            custody._recheck_parents(parents)
            if custody._file_identity(path.lstat()) != custody._file_identity(os.fstat(descriptor)):
                raise ProvisionRefusal('cortex_provisioning_conflict')
        except (OSError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_conflict')
    finally:
        if descriptor is not None:
            try:
                if acquired: fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class DeliveryJournal:
    """Private nonsecret receipt state; never recover or replay plaintext.

    The caller holds the operation lock. Recipient verification must authenticate
    each atomic native KeyStore record against its exact principal using the
    finite installed adapter. Metadata alone cannot establish that binding.
    """
    FIELDS = {'schema', 'installation_id', 'request_sha256', 'operation_id',
              'project_id', 'project_key', 'lead', 'console', 'state'}
    RECIPIENT = ('principal_id', 'manager', 'expires_at')

    def __init__(self, path: Path, args: dict, installation_id: str, store):
        self.path = path
        self.body = validate_request(args)
        self.installation = custody.canonical_uuid(installation_id)
        self.store = store
        request = json.dumps(self.body, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.request_sha256 = hashlib.sha256(request).hexdigest()
        self.started = False

    def _base(self):
        return {'schema': 'cortex.private-delivery.v1', 'installation_id': self.installation,
                'request_sha256': self.request_sha256, 'project_key': self.body['project_key']}

    def _read(self):
        try:
            custody._parents(self.path)
            if not self.path.exists() and not self.path.is_symlink(): return None
            value = custody.read_private_json(self.path)
            if (set(value) != self.FIELDS or any(value.get(k) != v for k, v in self._base().items())
                    or value['state'] not in ('command_started', 'delivery_pending', 'keys_committed')
                    or self.store.installation != self.installation or self.store.backend != 'file'):
                raise ValueError
            if value['state'] == 'command_started':
                if any(value[k] is not None for k in ('operation_id', 'project_id', 'lead', 'console')):
                    raise ValueError
            else:
                custody.canonical_uuid(value['operation_id']); custody.canonical_uuid(value['project_id'])
                for name in ('lead', 'console'):
                    if not isinstance(value[name], dict) or set(value[name]) != set(self.RECIPIENT):
                        raise ValueError
                    custody.canonical_uuid(value[name]['principal_id'])
            return value
        except (OSError, ValueError, TypeError, KeyError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_reissue_required') from None

    def begin(self) -> str:
        existing = self._read()
        if existing is not None:
            if existing['state'] == 'keys_committed': return 'resume'
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        if self.store.installation != self.installation or self.store.backend != 'file':
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        value = dict(self._base(), operation_id=None, project_id=None, lead=None,
                     console=None, state='command_started')
        custody.atomic_private_json(self.path, value)
        self.started = True
        return 'new'

    def _receipt(self, response: dict, state: str):
        return dict(self._base(), operation_id=response['operation_id'], project_id=response['project_id'],
                    lead={k: response['lead'][k] for k in self.RECIPIENT},
                    console={k: response['console'][k] for k in self.RECIPIENT}, state=state)

    def _records(self, response: dict, *, verify, issued: bool) -> bool:
        for name, label in (('lead', self.body['lead_name']), ('console', 'console')):
            record = self.store.read(self.body['project_key'], label)
            if (record is None or not isinstance(record.token, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{43}', record.token) is None
                    or record.metadata.managed_by != response[name]['manager']
                    or record.metadata.expires_at != response[name]['expires_at']):
                return False
            if issued and not hmac.compare_digest(record.token, response[name + '_token']): return False
            if verify(name, record, response[name]['principal_id']) is not True: return False
        return True

    def persist(self, response: dict, *, verify) -> None:
        try:
            issued = _response(response, self.body)
            current = self._read()
            if not issued or not self.started or current is None or current['state'] != 'command_started':
                raise ValueError
            # No second delivery attempt, even on the same in-memory object.
            self.started = False
            pending = self._receipt(response, 'delivery_pending')
            custody.atomic_private_json(self.path, pending)
            for name, label in (('lead', self.body['lead_name']), ('console', 'console')):
                self.store.put(self.body['project_key'], label, response[name + '_token'],
                               managed_by=response[name]['manager'], expires_at=response[name]['expires_at'])
            if not self._records(response, verify=verify, issued=True): raise ValueError
            custody.atomic_private_json(self.path, self._receipt(response, 'keys_committed'))
        except Exception:
            raise ProvisionRefusal('cortex_provisioning_reissue_required') from None

    def complete(self, response: dict, *, verify) -> bool:
        try:
            issued = _response(response, self.body)
            saved = self._read()
            if saved != self._receipt(response, 'keys_committed'): return False
            return self._records(response, verify=verify, issued=issued)
        except Exception:
            return False
