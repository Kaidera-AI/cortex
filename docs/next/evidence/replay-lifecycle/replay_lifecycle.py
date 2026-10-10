"""Mike PR35's accepted bounded synchronous cleanup pattern for own replays.

Completed create/lost acknowledgement is qualified. Arbitrarily late daemon
effects and a global engine lock are not. No application or host tests run here.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat


class Lifecycle:
    def __init__(self, root, runner):
        self.root, self.runner = Path(root), runner
        self.lifecycle = secrets.token_hex(16)
        self.pending, self.owned, self.binding = set(), set(), {}
        self.lock, self.password = None, None
        self.cleanup_verified = False
        self.events, self.attempts = [], []

    def labels(self):
        return ['--label', 'owner=cox@helix', '--label', 'kaidera.cox.lifecycle='+self.lifecycle]

    def _marker(self):
        return self.root / 'tmp' / 'cox-podman.lifecycle.json'

    def _fsync_directory(self):
        fd = os.open(self._marker().parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _snapshot(self):
        return {'version': 1, 'owner': 'cox@helix', 'lifecycle': self.lifecycle,
                'pending': [list(r) for r in sorted(self.pending)],
                'owned': [list(r) for r in sorted(self.owned)],
                'binding': [{'kind': kind, 'name': name, 'id': value}
                            for (kind, name), value in sorted(self.binding.items())]}

    def _write_marker(self):
        if self.lock is None:
            raise RuntimeError('cannot persist lifecycle without project lock')
        path = self._marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + secrets.token_hex(16) + '.tmp')
        payload = (json.dumps(self._snapshot(), sort_keys=True, separators=(',', ':')) + '\n').encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory()
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_marker(self):
        try:
            fd = os.open(self._marker(), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_uid != os.getuid():
                raise RuntimeError('dirty lifecycle marker ownership is unverified')
            raw = os.read(fd, 16385)
            if len(raw) > 16384:
                raise RuntimeError('dirty lifecycle marker is oversized')
            state = json.loads(raw)
        finally:
            os.close(fd)
        if not isinstance(state, dict) or set(state) != {'version', 'owner', 'lifecycle', 'pending', 'owned', 'binding'}:
            raise RuntimeError('dirty lifecycle marker shape is unverified')
        if state['version'] != 1 or state['owner'] != 'cox@helix' or not isinstance(state['lifecycle'], str) or not re.fullmatch('[0-9a-f]{32}', state['lifecycle']):
            raise RuntimeError('dirty lifecycle marker authority is unverified')
        resources = []
        for key in ('pending', 'owned'):
            if not isinstance(state[key], list):
                raise RuntimeError('dirty lifecycle resources are unverified')
            for row in state[key]:
                if not isinstance(row, list) or len(row) != 2 or row[0] not in ('pod', 'container') or not isinstance(row[1], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}', row[1]):
                    raise RuntimeError('dirty lifecycle resource identity is unverified')
                resources.append(tuple(row))
        if len(resources) != len(set(resources)) or not resources:
            raise RuntimeError('dirty lifecycle resource set is unverified')
        if not isinstance(state['binding'], list):
            raise RuntimeError('dirty lifecycle bindings are unverified')
        bindings = {}
        for row in state['binding']:
            if not isinstance(row, dict) or set(row) != {'kind', 'name', 'id'}:
                raise RuntimeError('dirty lifecycle binding shape is unverified')
            resource = (row['kind'], row['name'])
            if resource not in resources or resource in bindings or not isinstance(row['id'], str) or not re.fullmatch('[0-9a-f]{64}', row['id']):
                raise RuntimeError('dirty lifecycle immutable identity is unverified')
            bindings[resource] = row['id']
        if not {tuple(r) for r in state['owned']} <= set(bindings):
            raise RuntimeError('dirty owned resource lacks immutable binding')
        return state, bindings

    def _clear_marker(self):
        found = self._read_marker()
        if found is None:
            return
        state, _ = found
        if state['lifecycle'] != self.lifecycle:
            raise RuntimeError('cannot clear another lifecycle marker')
        self._marker().unlink()
        self._fsync_directory()

    def acquire(self):
        if self.lock is not None:
            raise RuntimeError('project lock already held')
        directory = self.root / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        candidate = (directory / 'cox-podman.lock').open('a')
        try:
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            candidate.close()
            raise
        self.lock = candidate
        original = (self.lifecycle, self.pending, self.owned, self.binding)
        try:
            found = self._read_marker()
            if found is not None:
                state, bindings = found
                self.lifecycle = state['lifecycle']
                self.pending = {tuple(r) for r in state['pending']}
                self.owned = {tuple(r) for r in state['owned']}
                self.binding = bindings
                errors = self._cleanup_resources()
                if errors:
                    self._write_marker()
                    raise RuntimeError('prior lifecycle cleanup incomplete: ' + '; '.join(errors))
                self._clear_marker()
                self.events.append({'event': 'dirty-lifecycle-reconciled', 'lifecycle': self.lifecycle})
                self.lifecycle, self.pending, self.owned, self.binding = original[0], set(), set(), {}
        except Exception:
            self.lifecycle, self.pending, self.owned, self.binding = original
            self.lock.close()
            self.lock = None
            raise

    def checked(self, args):
        value = self.runner(args)
        if value.returncode:
            raise RuntimeError('bounded replay operation failed: '+args[1])
        return value

    def inspect(self, kind, name):
        value = self.runner(['podman', kind, 'exists', name])
        if value.returncode == 1:
            bound = self.binding.get((kind, name))
            if bound is not None:
                identity = self.runner(['podman', kind, 'exists', bound])
                if identity.returncode == 0:
                    raise RuntimeError('bound immutable ID remains under another name')
                if identity.returncode != 1:
                    raise RuntimeError('bound immutable ID absence is unverified')
            return None
        if value.returncode != 0:
            raise RuntimeError('resource existence is unverified')
        rows = json.loads(self.checked(['podman', kind, 'inspect', name]).stdout)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError('resource identity is unverified')
        row = rows[0]
        actual = row['Name'].removeprefix('/')
        labels = row['Labels'] if kind == 'pod' else row['Config']['Labels']
        if actual != name:
            raise RuntimeError('inspected name differs from requested identity')
        if (labels or {}).get('owner') != 'cox@helix' or (labels or {}).get('kaidera.cox.lifecycle') != self.lifecycle:
            self.events.append({'event': 'foreign-preserved', 'kind': kind, 'name': name})
            return None
        target = row['Id']
        if not isinstance(target, str) or not re.fullmatch('[0-9a-f]{64}', target):
            raise RuntimeError('immutable removal identity is unverified')
        bound = self.binding.get((kind, name))
        if bound is not None and bound != target:
            raise RuntimeError('resource identity changed after acknowledgement')
        return row

    def create(self, kind, name, args):
        if self.lock is None:
            self.acquire()
        resource = kind, name
        self.pending.add(resource)  # Persist BEFORE the external effect or acknowledgement.
        self._write_marker()
        self.checked(args)
        if os.environ.get('REPLAY_POST_CREATE_FAULT') == kind:
            self.events.append({'event': 'completed-create-lost-ack-fault', 'kind': kind, 'name': name})
            raise RuntimeError('declared post-create acknowledgement fault')
        row = self.inspect(kind, name)
        if row is None:
            raise RuntimeError('create did not establish this lifecycle ownership')
        self.binding[resource] = row['Id']
        self.owned.add(resource)
        self.pending.remove(resource)
        self._write_marker()
        self.events.append({'event': 'created', 'kind': kind, 'name': name, 'id': row['Id']})
        return row['Id']

    def _cleanup_resources(self):
        errors = []
        resources = self.pending | self.owned
        order = sorted(resources, key=lambda r: (r[0] == 'pod', r[1]))
        for kind, name in order:
            resource = kind, name
            try:
                row = self.inspect(kind, name)
                if row is not None:
                    if kind == 'pod':
                        if any(r[0] == 'container' for r in self.pending | self.owned):
                            raise RuntimeError('container cleanup remains pending')
                        children = row.get('Containers')
                        if not isinstance(children, list):
                            raise RuntimeError('pod child inventory is unverified')
                        infra = row.get('InfraContainerID')
                        if any(child.get('Id') != infra for child in children):
                            raise RuntimeError('pod contains a foreign or unremoved child')
                    args = ['podman', 'pod', 'rm', '-f', row['Id']] if kind == 'pod' else ['podman', 'rm', '-f', row['Id']]
                    self.checked(args)
                    self.events.append({'event': 'removed', 'kind': kind, 'name': name, 'id': row['Id']})
                    if self.inspect(kind, name) is not None:
                        raise RuntimeError('owned resource remains after removal')
                self.pending.discard(resource)
                self.owned.discard(resource)
                self.binding.pop(resource, None)
            except Exception as error:
                errors.append(kind+': '+type(error).__name__+': '+str(error))
        return errors

    def close(self):
        errors = []
        self.cleanup_verified = False
        keep_lock = False
        try:
            if self.lock is None and (self.pending or self.owned):
                self.acquire()
            if self.lock is not None:
                errors = self._cleanup_resources()
                if errors:
                    self._write_marker()  # Durable dirty barrier BEFORE unlock.
                else:
                    self._clear_marker()
            self.cleanup_verified = not errors and not self.pending and not self.owned
        except Exception as error:
            errors.append(type(error).__name__+': '+str(error))
            keep_lock = self.lock is not None
        finally:
            self.password = None
            if self.lock is not None and not keep_lock:
                self.lock.close()
                self.lock = None
            self.attempts.append({'errors': errors, 'verified': self.cleanup_verified,
                                  'pending': sorted(self.pending), 'owned': sorted(self.owned),
                                  'password_discarded': self.password is None, 'lock_released': self.lock is None})
        if not self.cleanup_verified:
            raise RuntimeError('cleanup incomplete: '+'; '.join(errors))

    def finish(self):
        # A failed inspection/removal remains pending and receives a bounded retry.
        error = None
        for _ in range(2):
            try:
                self.close()
                error = None
                break
            except Exception as caught:
                error = caught
        return error

    def receipt(self):
        return {'lifecycle': self.lifecycle, 'cleanup_verified': self.cleanup_verified,
                'pending': sorted(self.pending), 'owned': sorted(self.owned),
                'events': self.events, 'attempts': self.attempts,
                'password_discarded': self.password is None, 'lock_released': self.lock is None}
