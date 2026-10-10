"""Core's signed pair admission; installer supplies the approved trust policy.

There is deliberately no default production pair. The released v0.1.002
launcher has no signed deployment aggregate, so it cannot be upgraded here
until the installer owner publishes and pins one.
"""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_MODULE_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
_VERSION = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
_UNSET = object()
_MANIFEST_FIELDS = frozenset({'format', 'release', 'edition', 'platform',
                              'schema_version', 'event_version', 'modules'})
_POLICY_FIELDS = frozenset({'trusted_key_sha256', 'old_release', 'new_release',
                            'old_manifest_sha256', 'new_manifest_sha256',
                            'edition', 'platform', 'old_schema', 'new_schema',
                            'old_event', 'new_event', 'old_modules', 'new_modules',
                            'expand_steps', 'readers', 'writers'})


class UpgradeRefusal(ValueError):
    """Typed refusal before any database migration or writer transition."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Admission:
    old_release: str
    new_release: str
    old_manifest_sha256: str
    new_manifest_sha256: str
    old_schema: int
    new_schema: int
    expand_steps: tuple[str, ...]
    readers: tuple[tuple[str, int], ...]
    writers: tuple[tuple[str, int], ...]

    def reader_allowed(self, release, schema_version):
        return (release, schema_version) in self.readers

    def writer_allowed(self, release, schema_version):
        # A new writer must never run against the old incompatible schema.
        return (release, schema_version) in self.writers and not (
            release == self.new_release and schema_version == self.old_schema)


def _unique_pairs(value):
    if not isinstance(value, list) or any(
        not isinstance(row, list) or len(row) != 2 or
        not isinstance(row[0], str) or type(row[1]) is not int
        for row in value
    ):
        raise UpgradeRefusal('invalid_policy')
    pairs = tuple((row[0], row[1]) for row in value)
    if len(set(pairs)) != len(pairs):
        raise UpgradeRefusal('invalid_policy')
    return pairs


def _policy(policy):
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise UpgradeRefusal('invalid_policy')
    if any(not isinstance(policy[key], str) or not _DIGEST.fullmatch(policy[key])
           for key in ('trusted_key_sha256', 'old_manifest_sha256',
                       'new_manifest_sha256')):
        raise UpgradeRefusal('invalid_policy')
    if any(type(policy[key]) is not int or policy[key] < 1
           for key in ('old_schema', 'new_schema', 'old_event', 'new_event')):
        raise UpgradeRefusal('invalid_policy')
    if (not isinstance(policy['expand_steps'], list)
        or not policy['expand_steps']
        or any(not isinstance(step, str) or not step for step in policy['expand_steps'])
        or len(set(policy['expand_steps'])) != len(policy['expand_steps'])):
        raise UpgradeRefusal('invalid_policy')
    for key in ('old_modules', 'new_modules'):
        modules = policy[key]
        if (not isinstance(modules, dict) or not modules
            or any(not isinstance(name, str) or not name or
                   not isinstance(digest, str) or not _MODULE_DIGEST.fullmatch(digest)
                   for name, digest in modules.items())):
            raise UpgradeRefusal('invalid_policy')
    _unique_pairs(policy['readers'])
    _unique_pairs(policy['writers'])


def _manifest(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise UpgradeRefusal('malformed_manifest')
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        if isinstance(error, UpgradeRefusal):
            raise
        raise UpgradeRefusal('malformed_manifest') from None
    if (not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS
        or json.dumps(value, sort_keys=True, separators=(',', ':')).encode() != raw
        or value['format'] != 'cortex-aggregate-v1'
        or any(not isinstance(value[k], str) or not value[k]
               for k in ('release', 'edition', 'platform'))
        or type(value['schema_version']) is not int
        or type(value['event_version']) is not int
        or not isinstance(value['modules'], dict)):
        raise UpgradeRefusal('malformed_manifest')
    return value


def _version(value):
    found = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        raise UpgradeRefusal('unknown_release')
    return tuple(map(int, found.groups()))


def _verified_bytes(manifest, signature, key, pinned_digest):
    try:
        raw = Path(manifest).read_bytes()
        if len(raw) > 1024 * 1024:
            raise UpgradeRefusal('malformed_manifest')
        if hashlib.sha256(raw).hexdigest() != pinned_digest:
            raise UpgradeRefusal('digest_mismatch')
        # Verify the exact bytes we hashed, even if the caller's path changes
        # while minisign is running.
        with tempfile.TemporaryDirectory(prefix='cortex-upgrade-verify-') as temporary:
            stable = Path(temporary) / 'manifest.json'
            stable.write_bytes(raw)
            result = subprocess.run(['minisign', '-V', '-q', '-p', str(key),
                                     '-m', str(stable), '-x', str(signature)],
                                    capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise UpgradeRefusal('signature_unavailable') from None
    if result.returncode != 0:
        raise UpgradeRefusal('signature_invalid')
    return raw


def admit_signed_pair(policy, old_manifest, old_signature,
                      new_manifest, new_signature, trusted_public_key):
    """Verify exact signed bytes and table compatibility before touching Core."""
    _policy(policy)
    try:
        key_bytes = Path(trusted_public_key).read_bytes()
    except OSError:
        raise UpgradeRefusal('untrusted_key') from None
    if hashlib.sha256(key_bytes).hexdigest() != policy['trusted_key_sha256']:
        raise UpgradeRefusal('untrusted_key')
    try:
        with tempfile.TemporaryDirectory(prefix='cortex-upgrade-key-') as temporary:
            os.chmod(temporary, 0o700)
            stable_key = Path(temporary) / 'trusted.pub'
            descriptor = os.open(stable_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(key_bytes)
                output.flush()
                os.fsync(output.fileno())
            old = _manifest(_verified_bytes(old_manifest, old_signature, stable_key,
                                            policy['old_manifest_sha256']))
            new = _manifest(_verified_bytes(new_manifest, new_signature, stable_key,
                                            policy['new_manifest_sha256']))
    except OSError:
        raise UpgradeRefusal('signature_unavailable') from None
    if (old['release'] != policy['old_release']
        or new['release'] != policy['new_release']):
        raise UpgradeRefusal('unknown_release')
    if _version(new['release']) <= _version(old['release']):
        raise UpgradeRefusal('downgrade')
    if (old['edition'] != new['edition'] or old['platform'] != new['platform']
        or old['edition'] != policy['edition'] or old['platform'] != policy['platform']):
        raise UpgradeRefusal('mixed_platform')
    if (old['schema_version'] != policy['old_schema']
        or new['schema_version'] != policy['new_schema']
        or old['schema_version'] < 1 or new['schema_version'] < old['schema_version']):
        raise UpgradeRefusal('unknown_schema')
    if (old['event_version'] != policy['old_event']
        or new['event_version'] != policy['new_event']
        or old['event_version'] < 1 or new['event_version'] < old['event_version']):
        raise UpgradeRefusal('unknown_event')
    if (old['modules'] != policy['old_modules']
        or new['modules'] != policy['new_modules']):
        raise UpgradeRefusal('module_set_incompatible')
    readers = _unique_pairs(policy['readers'])
    writers = _unique_pairs(policy['writers'])
    if ((old['release'], old['schema_version']) not in readers
        or (new['release'], new['schema_version']) not in readers
        or (old['release'], old['schema_version']) not in writers
        or (new['release'], new['schema_version']) not in writers
        or (new['release'], old['schema_version']) in writers):
        raise UpgradeRefusal('incompatible_clients')
    return Admission(old['release'], new['release'],
                     policy['old_manifest_sha256'], policy['new_manifest_sha256'],
                     old['schema_version'], new['schema_version'],
                     tuple(policy['expand_steps']), readers, writers)


class UpgradeCoordinator:
    """One-step, re-enterable upgrade through explicit installer-owned ports.

    The journal port must durably and atomically persist each save. The
    migration port's own ledger is the source of truth after a crash between
    applying a step and recording its completion here. Ports must be
    idempotent; Core never invokes an implicit startup migration.
    """

    def __init__(self, admission, ports):
        if admission is None or not getattr(admission, 'expand_steps', ()):
            raise UpgradeRefusal('admission_required')
        self.admission = admission
        self.ports = ports
        self.pair = (admission.old_manifest_sha256,
                     admission.new_manifest_sha256)

    def _state(self):
        state = self.ports.load()
        if state is not None and tuple(state['pair']) != self.pair:
            raise UpgradeRefusal('pair_mismatch')
        return state

    def prepare(self):
        state = self._state()
        if state is not None:
            if state['phase'] != 'prepared':
                raise UpgradeRefusal('fix_forward_only' if state['phase'] == 'accepted'
                                     else 'rollback_in_progress' if state['phase'] == 'rolling_back'
                                     else 'already_rolled_back')
            return
        self.ports.fence_old()
        if self.ports.verified_backup() is not True:
            raise UpgradeRefusal('backup_unverified')
        self.ports.save({'pair': self.pair, 'phase': 'prepared', 'steps': ()},
                        expected=None)

    def advance(self):
        state = self._state()
        if state is None or state['phase'] != 'prepared':
            raise UpgradeRefusal('not_prepared')
        applied = tuple(self.ports.applied_steps())
        steps = self.admission.expand_steps
        if applied != steps[:len(applied)] or len(applied) > len(steps):
            raise UpgradeRefusal('migration_ledger_diverged')
        if tuple(state['steps']) != applied:
            if tuple(state['steps']) != applied[:len(state['steps'])]:
                raise UpgradeRefusal('migration_ledger_diverged')
            self.ports.save({'pair': self.pair, 'phase': 'prepared', 'steps': applied},
                            expected=state)
            return applied[-1] if applied else None
        if len(applied) == len(steps):
            return None
        step = steps[len(applied)]
        self.ports.apply(through=step)
        after = tuple(self.ports.applied_steps())
        if after != steps[:len(applied) + 1]:
            raise UpgradeRefusal('migration_ledger_diverged')
        self.ports.save({'pair': self.pair, 'phase': 'prepared', 'steps': after},
                        expected=state)
        return step

    def accept(self):
        state = self._state()
        if state is None or state['phase'] != 'prepared':
            raise UpgradeRefusal('rollback_in_progress' if state and state['phase'] == 'rolling_back'
                                 else 'fix_forward_only' if state and state['phase'] == 'accepted'
                                 else 'not_prepared')
        if tuple(self.ports.applied_steps()) != self.admission.expand_steps:
            raise UpgradeRefusal('migrations_incomplete')
        if not self.admission.writer_allowed(self.admission.new_release,
                                              self.admission.new_schema):
            raise UpgradeRefusal('incompatible_clients')
        if self.ports.validate_preservation() is not True:
            raise UpgradeRefusal('preservation_failed')
        # This durable journal write is the fixed acceptance point. After it,
        # only forward repair is permitted, even if activation later fails.
        self.ports.save({'pair': self.pair, 'phase': 'accepted',
                         'steps': self.admission.expand_steps}, expected=state)

    def rollback(self):
        state = self._state()
        if state is None:
            raise UpgradeRefusal('not_prepared')
        if state['phase'] == 'accepted':
            raise UpgradeRefusal('fix_forward_only')
        if state['phase'] == 'rolled_back':
            raise UpgradeRefusal('already_rolled_back')
        if state['phase'] == 'prepared':
            self.ports.save({'pair': self.pair, 'phase': 'rolling_back',
                             'steps': tuple(state['steps'])}, expected=state)
            state = self._state()
        if state['phase'] != 'rolling_back':
            raise UpgradeRefusal('invalid_journal_transition')
        self.ports.fence_new()
        self.ports.restore()
        self.ports.save({'pair': self.pair, 'phase': 'rolled_back',
                         'steps': tuple(state['steps'])}, expected=state)


class AtomicUpgradeJournal:
    """Installer-supplied durable journal path, outside the DB restore set.

    No default path or production trust policy is supplied by Core. The
    caller owns custody of this file and must keep it across process restarts.
    """

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _normal(value):
        if (not isinstance(value, dict) or set(value) != {'pair', 'phase', 'steps'}
            or not isinstance(value['pair'], (tuple, list))
            or len(value['pair']) != 2
            or any(not isinstance(v, str) or not _DIGEST.fullmatch(v)
                   for v in value['pair'])
            or value['phase'] not in ('prepared', 'accepted', 'rolling_back', 'rolled_back')
            or not isinstance(value['steps'], (tuple, list))
            or any(not isinstance(v, str) or not v for v in value['steps'])):
            raise UpgradeRefusal('invalid_journal')
        return {'pair': tuple(value['pair']), 'phase': value['phase'],
                'steps': tuple(value['steps'])}

    def load(self):
        try:
            return self._normal(json.loads(self.path.read_bytes()))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError, OSError):
            raise UpgradeRefusal('invalid_journal') from None

    def save(self, state, *, expected=_UNSET):
        state = self._normal(state)
        if expected is not _UNSET and expected is not None:
            expected = self._normal(expected)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + '.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = self.load()
            if expected is not _UNSET and previous != expected:
                if previous is not None and previous['phase'] == 'accepted':
                    raise UpgradeRefusal('fix_forward_only')
                if previous is not None and previous['phase'] == 'rolling_back':
                    raise UpgradeRefusal('rollback_in_progress')
                if previous is not None and previous['phase'] == 'rolled_back':
                    raise UpgradeRefusal('already_rolled_back')
                raise UpgradeRefusal('journal_conflict')
            if previous is None:
                if state['phase'] != 'prepared' or state['steps']:
                    raise UpgradeRefusal('invalid_journal_transition')
            else:
                if previous['pair'] != state['pair']:
                    raise UpgradeRefusal('pair_mismatch')
                if previous['phase'] == 'accepted':
                    raise UpgradeRefusal('fix_forward_only')
                if previous['phase'] == 'rolled_back':
                    raise UpgradeRefusal('already_rolled_back')
                if previous['phase'] == 'rolling_back':
                    if (state['phase'] not in ('rolling_back', 'rolled_back')
                            or state['steps'] != previous['steps']):
                        raise UpgradeRefusal('rollback_in_progress')
                elif state['phase'] in ('prepared', 'accepted'):
                    if state['steps'][:len(previous['steps'])] != previous['steps']:
                        raise UpgradeRefusal('migration_ledger_diverged')
                elif state['phase'] == 'rolling_back':
                    if state['steps'] != previous['steps']:
                        raise UpgradeRefusal('migration_ledger_diverged')
                else:
                    raise UpgradeRefusal('invalid_journal_transition')
            raw = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
            name = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent,
                                                 prefix=self.path.name+'.',
                                                 delete=False) as temporary:
                    name = temporary.name
                    temporary.write(raw)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(name, self.path)
                folder = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            finally:
                if name is not None and os.path.exists(name):
                    os.unlink(name)
