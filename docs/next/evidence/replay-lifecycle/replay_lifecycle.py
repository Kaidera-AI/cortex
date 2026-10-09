"""Mike PR35's accepted bounded synchronous cleanup pattern for own replays.

Completed create/lost acknowledgement is qualified. Arbitrarily late daemon
effects and a global engine lock are not. No application or host tests run here.
"""
import fcntl
import json
from pathlib import Path
import re
import secrets


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

    def acquire(self):
        directory = self.root / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory / 'cox-podman.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def checked(self, args):
        value = self.runner(args)
        if value.returncode:
            raise RuntimeError('bounded replay operation failed: '+args[1])
        return value

    def inspect(self, kind, name):
        value = self.runner(['podman', kind, 'exists', name])
        if value.returncode == 1:
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
        resource = kind, name
        self.pending.add(resource)  # BEFORE the external effect or acknowledgement.
        self.checked(args)
        row = self.inspect(kind, name)
        if row is None:
            raise RuntimeError('create did not establish this lifecycle ownership')
        self.binding[resource] = row['Id']
        self.owned.add(resource)
        self.pending.remove(resource)
        self.events.append({'event': 'created', 'kind': kind, 'name': name, 'id': row['Id']})
        return row['Id']

    def close(self):
        errors = []
        self.cleanup_verified = False
        try:
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
                except Exception as error:
                    errors.append(kind+': '+type(error).__name__+': '+str(error))
            self.cleanup_verified = not errors and not self.pending and not self.owned
        finally:
            self.password = None
            if self.lock is not None:
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
