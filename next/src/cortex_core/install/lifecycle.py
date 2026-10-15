"""One P1 owner history and the private pre-consume lifecycle composition.

The installed composition is UNBOUND. Private source recorders can exercise
real durability in test-owned directories; they confer no native qualification.
P1 RecordRef/JournalEvent and B1's common policy remain the only wire/gate.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from cortex_core.release.codec import canonical_bytes, decode_json
from cortex_core.release.contracts import freeze, thaw

from . import owner
from .contracts import LIMITS, InstallRefusal, measure_original_ref, parse_install_message

_PREIMAGES = frozenset(('role', 'acl', 'database', 'hba', 'carrier'))
_LIMIT = 262144
_RAW_LIMIT = max(LIMITS.values())
_EVENT_NAME = re.compile(r'^[0-9]{20}\.json$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_OBSERVATIONS = frozenset(('context_ref', 'reservation_ref', 'attempt_ref', 'preimages_ref'))


def _require(value, detail):
    owner._require(value, detail)


def _decode(raw):
    try:
        value = decode_json(raw, _LIMIT)
    except (ValueError, TypeError, RecursionError):
        raise owner.OwnerRefusal('journal_invalid') from None
    _require(type(value) is dict, 'journal_invalid')
    return value


def _ref(path, raw):
    return {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class _History:
    """Private custody IO, never an authority obtained from a caller path.

    There is no head/attempt/replay database: current state is projected from
    this one immutable P1 event chain and its retained original fact bytes.
    The eventual installed owner supplies its protected root; no default exists.
    """

    def __init__(self, root):
        self.root = Path(root)
        _require(self.root.is_absolute(), 'custody_changed')
        try:
            self.root.mkdir(mode=0o700, exist_ok=True)
            with self._root() as fd:
                info = os.fstat(fd)
                self._identity = (info.st_dev, info.st_ino)
                for name in ('facts', 'events'):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = self._directory(fd, name)
                    os.close(child)
                os.fsync(fd)
        except owner.OwnerRefusal:
            raise
        except OSError:
            raise owner.OwnerRefusal('atomicity_unavailable') from None

    @contextmanager
    def _root(self):
        fd = None
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            _require(info.st_uid == os.geteuid() and not info.st_mode & 0o022, 'custody_changed')
            if hasattr(self, '_identity'):
                _require((info.st_dev, info.st_ino) == self._identity, 'custody_changed')
            yield fd
        except OSError:
            raise owner.OwnerRefusal('custody_changed') from None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _directory(root, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(fd)
            raise owner.OwnerRefusal('custody_changed')
        return fd

    @contextmanager
    def _lock(self, name):
        with self._root() as root:
            fd = None
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root)
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                         and info.st_nlink == 1 and not info.st_mode & 0o077, 'custody_changed')
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            except OSError:
                raise owner.OwnerRefusal('atomicity_unavailable') from None
            finally:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def operation(self):
        # Stable installation/operation/resource exclusion is distinct from CAS.
        # The fixed owner uses the same root for all operations it serializes.
        return self._lock('.operation.lock')

    def _read_path(self, folder, name):
        with self._root() as root:
            directory = self._directory(root, folder)
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                         and info.st_nlink == 1 and not info.st_mode & 0o077, 'custody_changed')
                with os.fdopen(fd, 'rb', closefd=False) as file:
                    raw = file.read(_RAW_LIMIT+1)
                _require(len(raw) <= _RAW_LIMIT, 'journal_invalid')
                return raw
            except OSError as error:
                detail = 'custody_changed' if error.errno == errno.ELOOP else 'journal_invalid'
                raise owner.OwnerRefusal(detail) from None
            finally:
                if fd is not None:
                    os.close(fd)
                os.close(directory)

    def read(self, reference):
        reference = thaw(reference)
        _require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}, 'journal_invalid')
        digest = reference['sha256']
        _require(type(digest) is str and _DIGEST.fullmatch(digest), 'journal_invalid')
        path = reference['path']
        _require(type(path) is str, 'journal_invalid')
        if path.startswith('events/'):
            name = path.removeprefix('events/')
            _require(_EVENT_NAME.fullmatch(name), 'journal_invalid')
            raw = self._read_path('events', name)
        else:
            # Original logical paths resolve only by their held content digest;
            # they can never select an arbitrary file in the owner's filesystem.
            raw = self._read_path('facts', digest+'.json')
        try:
            return measure_original_ref(reference, raw)
        except InstallRefusal:
            raise owner.OwnerRefusal('journal_invalid') from None

    def _publish(self, folder, name, raw):
        _require(type(raw) is bytes and len(raw) <= _RAW_LIMIT, 'journal_invalid')
        with self._root() as root:
            directory = self._directory(root, folder)
            temporary = '.pending-'+secrets.token_hex(16)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as file:
                    file.write(raw)
                    file.flush()
                    os.fsync(file.fileno())
                # Atomic no-clobber publication; originals and events are immutable.
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    _require(self._read_path(folder, name) == raw, 'journal_invalid')
                os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
            except OSError:
                raise owner.OwnerRefusal('atomicity_unavailable') from None
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)

    def retain(self, raw):
        _require(type(raw) is bytes and len(raw) <= _RAW_LIMIT, 'journal_invalid')
        path = 'facts/'+hashlib.sha256(raw).hexdigest()+'.json'
        self._publish('facts', path.split('/')[1], raw)
        return _ref(path, raw)

    def events(self):
        with self._root() as root:
            directory = self._directory(root, 'events')
            try:
                names = sorted(n for n in os.listdir(directory) if not n.startswith('.pending-'))
            finally:
                os.close(directory)
        result = []
        previous = None
        for sequence, name in enumerate(names):
            _require(name == f'{sequence:020d}.json', 'journal_invalid')
            raw = self._read_path('events', name)
            try:
                parsed = parse_install_message(raw, expected_kind='journal_event')
            except InstallRefusal:
                raise owner.OwnerRefusal('journal_invalid') from None
            event = thaw(parsed.document)
            _require(event['sequence'] == sequence and event['previous_event_ref'] == previous, 'journal_invalid')
            previous = _ref('events/'+name, raw)
            result.append((previous, event))
        return result

    def head(self):
        events = self.events()
        return events[-1] if events else (None, None)

    def append(self, event, *, expected_ref):
        with self._lock('.journal.lock'):
            current, previous = self.head()
            _require(current == expected_ref, 'generation_changed')
            sequence = 0 if previous is None else previous['sequence']+1
            _require(event['sequence'] == sequence and event['previous_event_ref'] == current, 'journal_invalid')
            raw = canonical_bytes(event)
            try:
                parse_install_message(raw, expected_kind='journal_event')
            except InstallRefusal:
                raise owner.OwnerRefusal('journal_invalid') from None
            # Resolve retained facts before making their event visible.
            for key in ('origin_request_ref', 'request_ref', 'release_binding_ref', 'observations_ref', 'result_ref'):
                if event[key] is not None:
                    self.read(event[key])
            path = f'events/{sequence:020d}.json'
            self._publish('events', path.split('/')[1], raw)
            return _ref(path, raw)

    def current_revision(self):
        reference, _ = self.head()
        if reference is None:
            return None
        _, obs = _load_entry(self, reference)
        reservation = _decode(self.read(obs['reservation_ref']))
        return reservation['expected_state_revision'] + (reservation['state'] != 'reserved')


def _load_entry(history, reference):
    event = thaw(parse_install_message(history.read(reference), expected_kind='journal_event').document)
    observation = _decode(history.read(event['observations_ref']))
    _require(set(observation) == _OBSERVATIONS, 'journal_invalid')
    for ref in observation.values():
        history.read(ref)
    return event, observation


def _operation(identity):
    return tuple(identity[k] for k in ('product', 'installation_id', 'operation_id'))


def _retained_preimages(history, identity):
    """PG-B2 private input: opaque P1 observations original, never a grant.

    The currently admitted dispatcher supplies this history and operation; the
    caller cannot select a root or use this ref as current backend authority.
    """
    for reference, event in history.events():
        if _operation(event['identity']) == _operation(identity):
            _, obs = _load_entry(history, reference)
            bundle = _decode(history.read(obs['preimages_ref']))
            _require(_operation(bundle['identity']) == _operation(identity), 'journal_invalid')
            return obs['preimages_ref']
    raise owner.OwnerRefusal('scope_denied')


def _latest_attempt(history, identity):
    for reference, event in reversed(history.events()):
        if event['identity'] == identity:
            _, obs = _load_entry(history, reference)
            return reference, event, obs
    raise owner.OwnerRefusal('journal_invalid')


def _publish_entry(history, *, context, reservation, attempt, preimages_ref, request_ref,
                   release_ref, expected_ref, phase, recorded_at, result_ref=None):
    observation = {'context_ref': history.retain(canonical_bytes(context)),
                   'reservation_ref': history.retain(canonical_bytes(reservation)),
                   'attempt_ref': history.retain(canonical_bytes(attempt)),
                   'preimages_ref': preimages_ref}
    previous, event = history.head()
    _require(previous == expected_ref, 'generation_changed')
    record = {'schema': 'kaidera.install-operation.v1', 'format_version': 1, 'kind': 'journal_event',
              'identity': context['identity'], 'sequence': 0 if event is None else event['sequence']+1,
              'previous_event_ref': expected_ref, 'origin_request_ref': attempt['origin_request_ref'],
              'request_ref': request_ref, 'release_binding_ref': release_ref, 'phase': phase,
              'effect': context['effect'], 'observations_ref': history.retain(canonical_bytes(observation)),
              'result_ref': result_ref,
              'recorded_at': datetime.fromtimestamp(recorded_at, timezone.utc).isoformat()}
    return history.append(record, expected_ref=expected_ref)


def _reserve(history, original_request, proposal, originals, preimages, *, clock_raw):
    context = thaw(proposal.context)
    request = parse_install_message(original_request, expected_kind='request')
    _require(thaw(request.document)['operation'] == 'install', 'scope_denied')
    measure_original_ref(context['request_ref'], original_request)
    _require(request.sha256 == proposal.request_sha256, 'scope_denied')
    current_ref, _ = history.head()
    revision = history.current_revision()
    if revision is not None:
        _require(revision == proposal.observed_state_revision, 'generation_changed')
    _require(proposal.observed_state_revision < owner._INT_MAX, 'generation_changed')
    _require(type(preimages) is dict and set(preimages) == _PREIMAGES
             and all(type(raw) is bytes and len(raw) <= _RAW_LIMIT for raw in preimages.values()), 'backend_unavailable')
    for reference, event in history.events():
        if event['identity'] == context['identity']:
            raise owner.OwnerRefusal('reservation_used')
        _, old_obs = _load_entry(history, reference)
        old_context = _decode(history.read(old_obs['context_ref']))
        if tuple(old_context[k] for k in ('owner_epoch','session_id','nonce')) == tuple(
                context[k] for k in ('owner_epoch','session_id','nonce')):
            raise owner.OwnerRefusal('nonce_replay')
        if (_operation(event['identity']) == _operation(context['identity'])
                and event['effect']['effect_id'] == context['effect']['effect_id']):
            _, _, latest_obs = _latest_attempt(history, event['identity'])
            old_attempt = _decode(history.read(latest_obs['attempt_ref']))
            detail = 'attempt_ambiguous' if old_attempt['dispatch_state'] == 'consumed_result_unknown' else 'reservation_used'
            raise owner.OwnerRefusal(detail)
    now = owner._decode(clock_raw, 'time_unavailable')
    owner._clock(now, context['owner_epoch'])
    _before_deadline(now, proposal.deadline.expires_at, proposal.deadline.expires_monotonic_ns)
    # These logical original refs are preserved byte-for-byte in C0 facts.
    history.retain(original_request)
    _retain_originals(history, originals, context)
    try:
        preimages_ref = _retained_preimages(history, context['identity'])
        existing = _decode(history.read(preimages_ref))
        _require(all(history.read(existing['originals'][k]) == preimages[k] for k in _PREIMAGES), 'resource_changed')
    except owner.OwnerRefusal as refusal:
        if refusal.detail != 'scope_denied':
            raise
        preimages_ref = history.retain(canonical_bytes({'identity': context['identity'],
            'originals': {k: history.retain(preimages[k]) for k in sorted(_PREIMAGES)}}))
    release_ref = history.retain(canonical_bytes(thaw(request.document)['release']))
    first_context_ref = history.retain(canonical_bytes(context))
    context['clock_ref'] = history.retain(clock_raw)
    context_ref = history.retain(canonical_bytes(context))
    reservation = {'reservation_id': secrets.token_hex(16), 'context_ref': context_ref,
        'effect_id': context['effect']['effect_id'], 'state': 'reserved', 'reserved_at': now['now_utc'],
        'expires_at': proposal.deadline.expires_at, 'expires_monotonic_ns': proposal.deadline.expires_monotonic_ns,
        'expected_state_revision': proposal.observed_state_revision+1}
    reservation_ref = history.retain(canonical_bytes(reservation))
    attempt = {'identity': context['identity'], 'origin_request_ref': context['request_ref'],
        'request_ref': context['request_ref'], 'context_ref': first_context_ref, 'reservation_ref': reservation_ref,
        'effect': context['effect'], 'predecessor_result_refs': [], 'dispatch_state': 'not_consumed',
        'result_ref': None, 'reconciliation_ref': None, 'consumption_sequence': None, 'consume_clock_ref': None}
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=preimages_ref, request_ref=context['request_ref'], release_ref=release_ref,
        expected_ref=current_ref, phase='create_intent', recorded_at=now['now_utc'])


def _before_deadline(now, utc, monotonic):
    _require(now['now_utc']+now['uncertainty_seconds'] < utc
             and now['monotonic_ns'] < monotonic, 'reservation_expired')


def _retain_originals(history, originals, context):
    _require(type(originals) is dict and len(originals) <= 256
             and all(type(raw) is bytes for raw in originals.values())
             and sum(map(len, originals.values())) <= 8*1024*1024, 'journal_invalid')
    for raw in originals.values():
        history.retain(raw)
    def held(value):
        if type(value) is dict:
            if set(value) == {'path','bytes','sha256'}:
                history.read(value)
            else:
                for child in value.values():
                    held(child)
        elif type(value) is list:
            for child in value:
                held(child)
    held(context)


def _continuity(original, context):
    _require({k:v for k,v in context.items() if k not in ('clock_ref','state_revision','current_state_ref')}
             == {k:v for k,v in original.items() if k not in ('clock_ref','state_revision','current_state_ref')},
             'reservation_invalidated')


def _consume(history, reserved_ref, current, clock_raw, *, originals):
    event, obs = _load_entry(history, reserved_ref)
    latest_ref, _, latest = _latest_attempt(history, event['identity'])
    reservation = _decode(history.read(latest['reservation_ref']))
    _require(reservation['state'] == 'reserved', 'reservation_used')
    _require(latest_ref == reserved_ref, 'reservation_invalidated')
    _require(current.observed_state_revision == reservation['expected_state_revision'], 'generation_changed')
    _require(current.observed_state_revision < owner._INT_MAX, 'generation_changed')
    original = _decode(history.read(obs['context_ref']))
    context = thaw(current.context)
    _continuity(original, context)
    now = owner._decode(clock_raw, 'time_unavailable')
    owner._clock(now, original['owner_epoch'])
    _before_deadline(now, min(reservation['expires_at'], current.deadline.expires_at),
                     min(reservation['expires_monotonic_ns'], current.deadline.expires_monotonic_ns))
    _retain_originals(history, originals, context)
    context['clock_ref'] = history.retain(clock_raw)
    attempt = _decode(history.read(obs['attempt_ref']))
    reservation['state'] = 'consumed'
    reservation_ref = history.retain(canonical_bytes(reservation))
    attempt.update(reservation_ref=reservation_ref, dispatch_state='consumed_result_unknown',
                   consumption_sequence=event['sequence']+1, consume_clock_ref=history.retain(clock_raw))
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=obs['preimages_ref'], request_ref=event['request_ref'],
        release_ref=event['release_binding_ref'], expected_ref=reserved_ref,
        phase='create_intent', recorded_at=now['now_utc'])


def _record_result(history, consumed_ref, original_measurement, *, current_context=None,
                   originals=None, clock_raw=None, result_originals=None):
    event, obs = _load_entry(history, consumed_ref)
    latest_ref, _, latest = _latest_attempt(history, event['identity'])
    _require(latest_ref == consumed_ref, 'reservation_used')
    reservation = _decode(history.read(obs['reservation_ref']))
    attempt = _decode(history.read(obs['attempt_ref']))
    _require(reservation['state'] == 'consumed' and attempt['dispatch_state'] == 'consumed_result_unknown', 'reservation_used')
    result = _decode(original_measurement)
    _require(set(result) == {'identity','effect','request_ref','state','observed'}, 'journal_invalid')
    _require(result['identity'] == event['identity'] and result['effect'] == event['effect']
             and result['request_ref'] == event['request_ref'], 'resource_changed')
    _require(result['state'] in ('applied_verified','not_applied_verified') and type(result['observed']) is dict,
             'attempt_ambiguous')
    result_ref = history.retain(original_measurement)
    attempt.update(dispatch_state=result['state'], result_ref=result_ref)
    context = _decode(history.read(obs['context_ref']))
    if current_context is not None:
        _continuity(context, current_context)
        context = current_context
        _retain_originals(history, originals, context)
    clock_raw = history.read(attempt['consume_clock_ref']) if clock_raw is None else clock_raw
    clock = owner._decode(clock_raw, 'time_unavailable')
    owner._clock(clock, context['owner_epoch'])
    if result_originals is not None:
        _retain_originals(history, result_originals, {})
    history.read(clock['clock_receipt_ref'])
    context['clock_ref'] = history.retain(clock_raw)
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=obs['preimages_ref'], request_ref=event['request_ref'],
        release_ref=event['release_binding_ref'], expected_ref=consumed_ref,
        phase='created' if result['state'] == 'applied_verified' else 'held',
        recorded_at=clock['now_utc'], result_ref=result_ref)


@dataclass(frozen=True)
class _Bindings:
    history: _History
    primitives: dict


def _installed_lifecycle():
    # The one planned lifecycle IO/finite-adapter port, not another owner gate.
    raise owner.OwnerRefusal('atomicity_unavailable')


def execute_effect(original_request):
    """Consume B1's DATA in the fixed owner; no caller root/callback/provider."""
    first = owner.propose_effect(original_request)
    bindings = _installed_lifecycle()
    _require(type(bindings) is _Bindings and type(bindings.history) is _History
             and type(bindings.primitives) is dict, 'backend_unavailable')
    history = bindings.history
    with history.operation():
        inputs = owner._installed_owner_inputs()
        current = owner._evaluate(original_request, inputs, previous_deadline=first.deadline)
        owner._same_context(first, current)
        primitive = bindings.primitives.get(current.context['effect']['resource_kind'])
        _require(primitive is not None, 'backend_unavailable')
        preimages = primitive.snapshot()
        inputs = owner._installed_owner_inputs()
        refreshed = owner._evaluate(original_request, inputs, previous_deadline=current.deadline)
        owner._same_context(current, refreshed)
        reserved = _reserve(history, original_request, refreshed, inputs.originals, preimages,
                            clock_raw=inputs.current_clock_raw)
        inputs = owner._installed_owner_inputs()
        final = owner._evaluate(original_request, inputs, previous_deadline=refreshed.deadline)
        consumed = _consume(history, reserved, final, inputs.current_clock_raw, originals=inputs.originals)
        _, observation = _load_entry(history, consumed)
        # Consume persistence can block. Recheck the common owner at the actual
        # dispatch point; the consumed marker alone cannot authorize dispatch.
        dispatch_inputs = owner._installed_owner_inputs()
        dispatch_current = owner._evaluate(original_request, dispatch_inputs, previous_deadline=final.deadline)
        _require(dispatch_current.observed_state_revision == final.observed_state_revision+1, 'generation_changed')
        _continuity(thaw(final.context), thaw(dispatch_current.context))
        effect_context = freeze({'context': thaw(dispatch_current.context), 'journal_ref': consumed,
                                 'preimages_ref': observation['preimages_ref']})
        try:
            primitive.dispatch(effect_context)
            measured = primitive.measure(effect_context)
            result_inputs = owner._installed_owner_inputs()
            completed = _record_result(history, consumed, measured,
                current_context=thaw(dispatch_current.context), originals=dispatch_inputs.originals,
                clock_raw=result_inputs.current_clock_raw, result_originals=result_inputs.originals)
        except owner.OwnerRefusal:
            raise
        except Exception:
            raise owner.OwnerRefusal('attempt_ambiguous') from None
        # Return only the existing immutable P1 event reference. It is neither a
        # client receipt nor a bearer permit; the owner retains its original facts.
        return freeze(completed)
