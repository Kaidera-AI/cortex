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
import threading
import time

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
        self._publication = threading.local()
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
    def _lock(self, name, *, deadline=None, expected_context=None):
        with self._root() as root:
            fd = None
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root)
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                         and info.st_nlink == 1 and not info.st_mode & 0o077, 'custody_changed')
                if deadline is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    inputs, admitted = _admitted_deadline(deadline, expected_context=expected_context)
                    if expected_context is not None:
                        _require(expected_context['state_revision'] == admitted.observed_state_revision,
                                 'generation_changed')
                    now = owner._decode(inputs.current_clock_raw,'time_unavailable')
                    budget = min((deadline.expires_at-now['now_utc']-now['uncertainty_seconds'])*1_000_000_000,
                                 deadline.expires_monotonic_ns-now['monotonic_ns'])
                    stop = time.monotonic_ns()+budget
                    while True:
                        _, fresh = _admitted_deadline(deadline, expected_context=expected_context)
                        owner._same_context(admitted,fresh)
                        _require(time.monotonic_ns() < stop,'reservation_expired')
                        try:
                            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(min(.01,max(0,(stop-time.monotonic_ns())/1_000_000_000)))
                    _, fresh = _admitted_deadline(deadline, expected_context=expected_context)
                    owner._same_context(admitted,fresh)
                    _require(time.monotonic_ns() < stop,'reservation_expired')
                yield stop if deadline is not None else None
            except OSError:
                raise owner.OwnerRefusal('atomicity_unavailable') from None
            finally:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    @contextmanager
    def operation(self, *, deadline=None, expected_context=None):
        # One resource lock and one original owner/deadline for this operation.
        # Thread-local scope only carries private DATA to existing persistence;
        # it is neither another supplier nor an installed authority capability.
        with self._lock('.operation.lock', **({} if deadline is None else {
                'deadline': deadline, 'expected_context': expected_context})) as stop:
            previous = getattr(self._publication, 'current', None)
            if deadline is not None:
                if previous is not None:
                    _require(previous[0] == deadline, 'reservation_expired')
                    if previous[1] is not None and expected_context is not None:
                        _continuity(previous[1], expected_context)
                self._publication.current = (deadline, expected_context,
                    stop if previous is None else min(stop, previous[2]))
            try:
                yield
                # Final owner/history reads can return after the original stop.
                # Refuse a late successful ACK while retaining durable facts.
                if deadline is not None:
                    self._check_publication(self.head()[0])
            finally:
                self._publication.current = previous

    def _check_publication(self, expected_head=None, *, check_chain=True):
        scope = getattr(self._publication, 'current', None)
        if scope is None:
            return  # Unbound private source recorders, never the fixed entry.
        deadline, context, stop = scope
        _require(time.monotonic_ns() < stop, 'reservation_expired')
        if not check_chain:
            # Retained facts are ordinary evidence, not a published decision.
            # The event/child ACK re-admits after all their persistence waits.
            return
        _require(self.head()[0] == expected_head, 'generation_changed')
        revision = self.current_revision()
        _, current = _admitted_deadline(deadline, expected_context=context)
        if revision is not None:
            _require(current.observed_state_revision == revision, 'generation_changed')
        elif context is not None:
            _require(current.observed_state_revision == context['state_revision'], 'generation_changed')
        _require(self.head()[0] == expected_head, 'generation_changed')
        # Reads/observation can wait too; retain the operation's original cutoff.
        _require(time.monotonic_ns() < stop, 'reservation_expired')

    def _read_path(self, folder, name, *, missing_ok=False):
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
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise owner.OwnerRefusal('journal_invalid') from None
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
        _require(type(reference['path']) is str, 'journal_invalid')
        # Logical originals never select a physical journal path, including
        # valid P1 names that happen to start with events/.
        raw = self._read_path('facts', digest+'.json', missing_ok=True)
        if raw is None:
            journal_ref = next((ref for ref, _ in self.events() if ref == reference), None)
            _require(journal_ref is not None, 'journal_invalid')
            # Only a complete reference from the verified chain selects an
            # event. Reread its original bytes; never reserialize the record.
            raw = self._read_path('events', journal_ref['path'].removeprefix('events/'))
        try:
            return measure_original_ref(reference, raw)
        except InstallRefusal:
            raise owner.OwnerRefusal('journal_invalid') from None

    def _publish(self, folder, name, raw):
        _require(type(raw) is bytes and len(raw) <= _RAW_LIMIT, 'journal_invalid')
        guarded = getattr(self._publication, 'current', None) is not None
        publication_head = self.head()[0] if guarded and folder == 'events' else None
        if guarded and folder == 'events':
            self._check_publication(publication_head)
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
                # File fsync may wait: re-admit before exposing a chain event.
                if guarded and folder == 'events':
                    self._check_publication(publication_head)
                # Atomic no-clobber publication; originals and events are immutable.
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    _require(self._read_path(folder, name) == raw, 'journal_invalid')
                os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
                if guarded:
                    # Ordinary original bytes may survive lost authority as DATA.
                    # A late observation never ACKs a stale event or receipt.
                    expected = _ref('events/'+name, raw) if folder == 'events' else publication_head
                    self._check_publication(expected, check_chain=folder == 'events')
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

    def append(self, event, *, expected_ref, deadline=None, expected_context=None):
        observation = _decode(self.read(event['observations_ref']))
        attempt = _decode(self.read(observation['attempt_ref']))
        if attempt.get('sql_mandate_ref') is not None:
            _require(type(deadline) is owner.ProposedDeadline,'time_unavailable')
        with self._lock('.journal.lock',**({} if deadline is None else {
                'deadline':deadline, 'expected_context':expected_context})):
            current, previous = self.head()
            _require(current == expected_ref, 'generation_changed')
            if expected_context is not None:
                _, admitted = _admitted_deadline(deadline, expected_context=expected_context)
                _require(admitted.observed_state_revision == expected_context['state_revision']
                    and self.current_revision() in (None,admitted.observed_state_revision),
                    'generation_changed')
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
            if expected_context is not None:
                _, final = _admitted_deadline(deadline, expected_context=expected_context)
                owner._same_context(admitted,final)
                _require(self.head()[0] == expected_ref
                    and self.current_revision() in (None,final.observed_state_revision),
                    'generation_changed')
            self._publish('events', path.split('/')[1], raw)
            if deadline is not None:
                _admitted_deadline(deadline, expected_context=expected_context)
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


def _admitted_deadline(deadline, *, expected_context=None):
    """Reobserve the one fixed owner; a private timeout is never authority."""
    _require(type(deadline) is owner.ProposedDeadline, 'time_unavailable')
    inputs = _owner_inputs()
    context = owner._decode(inputs.context_raw, 'owner_unavailable')
    if expected_context is not None:
        _require(type(expected_context) is dict, 'owner_unavailable')
        context = expected_context
    try:
        request = inputs.originals[context['request_ref']['path']]
    except (KeyError, TypeError):
        raise owner.OwnerRefusal('owner_unavailable') from None
    owner._release_original(request, inputs)
    proposal = owner._evaluate(request, inputs, previous_deadline=deadline)
    _require(proposal.deadline == deadline, 'reservation_expired')
    if expected_context is not None:
        _continuity(expected_context,thaw(proposal.context))
    return inputs, proposal


def _sql_mandate(history, raw, context):
    mandate = _decode(raw)
    _require(raw == canonical_bytes(mandate) and set(mandate) == {
        'protocol_version','identity','effect','request_ref','phase',
        'phase_before_ref','native_binding_ref'} and type(mandate['protocol_version']) is int
        and mandate['protocol_version'] == 2,
        'journal_invalid')
    _require(all(mandate[k] == context[k] for k in ('identity','effect','request_ref')),
             'scope_denied')
    _require(type(mandate['phase']) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',mandate['phase']),
             'journal_invalid')
    before = _decode(history.read(mandate['phase_before_ref']))
    _require(set(before) == {'originals'} and type(before['originals']) is dict
        and set(before['originals']) == _PREIMAGES, 'journal_invalid')
    for reference in before['originals'].values():history.read(reference)
    history.read(mandate['native_binding_ref'])
    return mandate


def _sql_prefix(history, consumed_ref, publication_head, *, candidate=None):
    """ONE private rule for ordered descendants of an original consumption.

    SQL outcome/POSTIMAGE meaning is the fixed producer's job. Here, tentative
    DATA never becomes terminal truth, a stale head is never proof, and every
    original subject/reservation/preimage remains bound to its consumption.
    """
    events = history.events()
    _require(events and events[-1][0] == publication_head, 'generation_changed')
    indices = [i for i,(ref,_) in enumerate(events) if ref == consumed_ref]
    _require(len(indices) == 1, 'journal_invalid')
    index = indices[0]
    def loaded(reference, event):
        _, obs = _load_entry(history,reference)
        return {'ref':reference,'event':event,'preimages_ref':obs['preimages_ref'],
            **{k:_decode(history.read(obs[k+'_ref'])) for k in ('context','reservation','attempt')}}
    root = loaded(*events[index]);a=root['attempt'];r=root['reservation'];e=root['event']
    _require(index > 0 and a.get('sql_mandate_ref') is not None
        and a.get('sql_xact_ref') is None and a.get('sql_postimage_ref') is None
        and a['dispatch_state'] == 'consumed_result_unknown' and r['state'] == 'consumed'
        and type(a['consumption_sequence']) is int and a['consumption_sequence'] == e['sequence']
        and a['result_ref'] is None and a['reconciliation_ref'] is None, 'journal_invalid')
    _sql_mandate(history,history.read(a['sql_mandate_ref']),root['context'])
    _require(root['preimages_ref'] == _retained_preimages(history,e['identity']), 'journal_invalid')
    previous = loaded(*events[index-1]);prior=previous['attempt'];reserved=previous['reservation']
    _require(all(previous['event'][k] == e[k] for k in
            ('identity','effect','request_ref','origin_request_ref','release_binding_ref'))
        and reserved['state'] == 'reserved'
        and {**reserved,'state':'consumed'} == r and prior['dispatch_state'] == 'not_consumed'
        and prior['consumption_sequence'] is None and prior['consume_clock_ref'] is None
        and {**prior,'reservation_ref':a['reservation_ref'],'dispatch_state':'consumed_result_unknown',
             'consumption_sequence':e['sequence'],'consume_clock_ref':a['consume_clock_ref']} == a
        and previous['preimages_ref'] == root['preimages_ref'], 'journal_invalid')
    _continuity(previous['context'],root['context'])
    _require(all(a[k] == e[k] == root['context'][k] for k in ('identity','effect','request_ref')),
             'journal_invalid')
    clock = owner._decode(history.read(a['consume_clock_ref']),'time_unavailable')
    owner._clock(clock,root['context']['owner_epoch'])
    _before_deadline(clock,r['expires_at'],r['expires_monotonic_ns'])
    descendants = [loaded(ref,event) for ref,event in events[index+1:]]
    if candidate is not None:descendants.append(candidate)
    previous = root
    for position,current in enumerate(descendants):
        event,attempt,reservation,context = (current[k] for k in ('event','attempt','reservation','context'))
        old = previous['attempt']
        _require(all(event[k] == e[k] for k in
            ('identity','effect','request_ref','origin_request_ref','release_binding_ref'))
            and current['preimages_ref'] == root['preimages_ref'], 'journal_invalid')
        _continuity(root['context'],context)
        _require(type(attempt['consumption_sequence']) is int, 'journal_invalid')
        fixed = {'sql_xact_ref','sql_postimage_ref','dispatch_state','result_ref',
                 'reconciliation_ref','reservation_ref'}
        _require({k:v for k,v in attempt.items() if k not in fixed}
            == {k:v for k,v in a.items() if k not in fixed}, 'journal_invalid')
        _require(previous['attempt']['dispatch_state'] == 'consumed_result_unknown', 'reservation_used')
        if attempt['dispatch_state'] == 'consumed_result_unknown':
            _require(attempt['result_ref'] is None and attempt['reconciliation_ref'] is None
                and event['result_ref'] is None and reservation == r
                and attempt['reservation_ref'] == a['reservation_ref']
                and context == root['context'] and event['phase'] == e['phase'], 'journal_invalid')
            xid,post = attempt.get('sql_xact_ref'),attempt.get('sql_postimage_ref')
            if old.get('sql_xact_ref') is None and xid is not None and post is None:
                stage,reference = 'xid',xid
            elif old.get('sql_xact_ref') is not None and xid == old['sql_xact_ref'] \
                    and old.get('sql_postimage_ref') is None and post is not None:
                stage,reference = 'postimage',post
            else:raise owner.OwnerRefusal('journal_invalid')
            raw=history.read(reference);fact=_decode(raw)
            _require(raw == canonical_bytes(fact) and set(fact) == {
                'stage','consumed_ref','sql_mandate_ref','sql_xact_ref','observed'}
                and fact['stage'] == stage and fact['consumed_ref'] == consumed_ref
                and fact['sql_mandate_ref'] == a['sql_mandate_ref']
                and fact['sql_xact_ref'] == (xid if stage == 'postimage' else None)
                and type(fact['observed']) is dict, 'journal_invalid')
            from . import recovery
            recovery._read_refs(history,fact['observed'])
        else:
            _require(position == len(descendants)-1 and attempt['dispatch_state'] in
                ('applied_verified','not_applied_verified')
                and all(attempt.get(k) == old.get(k) for k in ('sql_xact_ref','sql_postimage_ref'))
                and event['result_ref'] is not None and attempt['result_ref'] == event['result_ref']
                and attempt['reconciliation_ref'] in (None,event['result_ref']), 'journal_invalid')
            _require(history.read(attempt['reservation_ref']) == canonical_bytes(reservation),
                     'journal_invalid')
            result=_decode(history.read(event['result_ref']))
            _require(set(result) == {'identity','effect','request_ref','state','observed'}
                and all(result[k] == e[k] for k in ('identity','effect','request_ref'))
                and result['state'] == attempt['dispatch_state'] and type(result['observed']) is dict
                and result['observed'].get('sql_checkpoint_head') == previous['ref']
                and (result['state'] != 'applied_verified' or attempt.get('sql_postimage_ref') is not None),
                'journal_invalid')
            _require(reservation == {**r,'expected_state_revision':r['expected_state_revision']+1}
                and context['state_revision'] == reservation['expected_state_revision'], 'generation_changed')
        previous=current
    return previous


class _X11:
    """Private projection of an already-open fixed X11 primitive into P1.

    The caller cannot install this via request DATA. Its native supplier's hold
    must enclose execute_effect, including operation's final ACK. No effects,
    result publisher or new admission path are introduced by this projection.
    """
    def __init__(self, primitive, history):
        from . import files, podman
        _require(type(primitive) in (files._Primitive, podman._Primitive)
            and type(history) is _History, 'backend_unavailable')
        self.primitive, self.history = primitive, history
        self.before = None

    def snapshot(self):
        value = self.primitive.snapshot()
        kind = self.primitive.plan['subject']['effect']['resource_kind']
        self.before = {kind: canonical_bytes(thaw(value))}
        return dict(self.before)

    def _subject(self, effect):
        effect = thaw(effect)
        context = effect['context']
        subject = {k: context[k] for k in ('identity', 'effect', 'request_ref')}
        _require(subject == thaw(self.primitive.plan)['subject'], 'scope_denied')
        event, obs = _load_entry(self.history, effect['journal_ref'])
        _require(all(event[k] == subject[k] for k in subject)
            and obs['preimages_ref'] == effect['preimages_ref'], 'journal_invalid')
        reservation = _decode(self.history.read(obs['reservation_ref']))
        deadline = effect.get('deadline')
        _require(type(deadline) is owner.ProposedDeadline
            and self.primitive.plan['deadline_ns'] <= min(deadline.expires_monotonic_ns,
                reservation['expires_monotonic_ns']), 'reservation_expired')
        before = _decode(self.history.read(obs['preimages_ref']))
        _require(before['identity'] == subject['identity'] and self.before is not None
            and set(before['originals']) == set(self.before)
            and all(self.history.read(before['originals'][k]) == raw
                    for k, raw in self.before.items()), 'resource_changed')
        return subject

    def dispatch(self, effect):
        self.primitive.dispatch(self._subject(effect))

    def measure(self, effect):
        return self.primitive.measure(self._subject(effect))


@contextmanager
def _measurement(primitive, context, *, mandated, recovery=False):
    # ONE held measurement path for SQL and X11. Legacy private source
    # recorders retain their immediate-byte/tuple contract; native X11 never
    # loses its hold at the tuple-to-normal-result projection.
    measured = primitive.measure(context)
    held = hasattr(measured, '__enter__') and hasattr(measured, '__exit__')
    _require(not (mandated or type(primitive) is _X11) or held, 'atomicity_unavailable')
    def project(answer):
        if not recovery and type(answer) is tuple:
            _require(len(answer) == 2 and answer[0] in ('APPLIED', 'NOT_APPLIED')
                and type(answer[1]) is bytes, 'attempt_ambiguous')
            return answer[1]
        return answer
    if held:
        failure = None
        with measured as answer:
            try: yield project(answer)
            except BaseException as error:
                failure = error
                raise
        # A producer must never turn failed publication/HOLD into an ACK.
        if failure is not None: raise failure
    else:
        yield project(measured)


def _publish_entry(history, *, context, reservation, attempt, preimages_ref, request_ref,
                   release_ref, expected_ref, phase, recorded_at, result_ref=None, deadline=None,
                   expected_context=None):
    if attempt.get('sql_mandate_ref') is not None:
        if expected_context is None:
            # Historical checkpoint context identifies the request; its cursor is DATA.
            _, admitted = _admitted_deadline(deadline, expected_context=context)
            expected_context = thaw(admitted.context)
        else:
            _admitted_deadline(deadline, expected_context=expected_context)
        _sql_mandate(history,history.read(attempt['sql_mandate_ref']),context)
        sequence = attempt['consumption_sequence']
        _require(sequence is None or (type(sequence) is int and sequence >= 0),'journal_invalid')
        events = history.events()
        if sequence is not None and sequence < len(events):
            candidate = {'ref':None,'event':{'identity':context['identity'],'effect':context['effect'],
                'request_ref':request_ref,'origin_request_ref':attempt['origin_request_ref'],
                'release_binding_ref':release_ref,'phase':phase,'result_ref':result_ref},
                'context':context,'reservation':reservation,'attempt':attempt,'preimages_ref':preimages_ref}
            _sql_prefix(history,events[sequence][0],expected_ref,candidate=candidate)
        else:
            _require(attempt.get('sql_xact_ref') is None and attempt.get('sql_postimage_ref') is None
                and attempt['result_ref'] is None and attempt['reconciliation_ref'] is None
                and result_ref is None,'journal_invalid')
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
    return history.append(record, expected_ref=expected_ref,**({} if deadline is None else {
        'deadline':deadline, 'expected_context':expected_context}))


def _reserve(history, original_request, proposal, originals, preimages, *, clock_raw, inputs=None,
             sql_mandate_raw=None, deadline=None):
    context = thaw(proposal.context)
    if sql_mandate_raw is not None:
        _, admitted = _admitted_deadline(deadline, expected_context=context)
        owner._same_context(proposal,admitted)
        _require(proposal.deadline == deadline,'reservation_expired')
        _sql_mandate(history,sql_mandate_raw,context)
    request = parse_install_message(original_request, expected_kind='request')
    operation = thaw(request.document)['operation']
    _require(operation in ('install', 'rollback', 'uninstall', 'purge'), 'scope_denied')
    measure_original_ref(context['request_ref'], original_request)
    _require(request.sha256 == proposal.request_sha256, 'scope_denied')
    current_ref, _ = history.head()
    revision = history.current_revision()
    if revision is not None:
        _require(revision == proposal.observed_state_revision, 'generation_changed')
    _require(proposal.observed_state_revision < owner._INT_MAX, 'generation_changed')
    preimage_names = set(preimages) if type(preimages) is dict else set()
    _require(type(preimages) is dict and (preimage_names == _PREIMAGES
             or (sql_mandate_raw is None and preimage_names == {context['effect']['resource_kind']}))
             and all(type(raw) is bytes and len(raw) <= _RAW_LIMIT for raw in preimages.values()), 'backend_unavailable')
    lineage = None
    retry = operation == 'install' and thaw(request.document)['journal_ref'] is not None
    if retry:
        from . import recovery
        lineage = recovery._retry_parent(history,thaw(request.document))
        recovery._renewed_child(history,proposal,inputs,lineage)
    for reference, event in history.events():
        if retry:
            _require(all(context['identity'][k] != event['identity'][k] for k in ('request_id','attempt_id')),
                     'reservation_used')
        if event['identity'] == context['identity']:
            raise owner.OwnerRefusal('reservation_used')
        _, old_obs = _load_entry(history, reference)
        old_context = _decode(history.read(old_obs['context_ref']))
        if retry:
            _require(context['nonce'] != old_context['nonce'],'nonce_replay')
        if tuple(old_context[k] for k in ('owner_epoch','session_id','nonce')) == tuple(
                context[k] for k in ('owner_epoch','session_id','nonce')):
            raise owner.OwnerRefusal('nonce_replay')
        if (_operation(event['identity']) == _operation(context['identity'])
                and event['effect']['effect_id'] == context['effect']['effect_id']):
            if retry and event['identity'] in [p['event']['identity'] for p in [lineage]+lineage['ancestors']]:
                continue
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
    if operation != 'install':
        _require(type(inputs) is owner._Inputs and inputs.originals == originals
                 and inputs.current_clock_raw == clock_raw, 'owner_unavailable')
        lineage, _ = _part1_plan(history, original_request, proposal, inputs)
    try:
        preimages_ref = _retained_preimages(history, context['identity'])
        existing = _decode(history.read(preimages_ref))
        _require(set(existing['originals']) == preimage_names
            and all(history.read(existing['originals'][k]) == preimages[k] for k in preimage_names), 'resource_changed')
    except owner.OwnerRefusal as refusal:
        if refusal.detail != 'scope_denied':
            raise
        preimages_ref = history.retain(canonical_bytes({'identity': context['identity'],
            'originals': {k: history.retain(preimages[k]) for k in sorted(preimage_names)}}))
    release_ref = history.retain(canonical_bytes(thaw(request.document)['release']))
    first_context_ref = history.retain(canonical_bytes(context))
    context['clock_ref'] = history.retain(clock_raw)
    context_ref = history.retain(canonical_bytes(context))
    reservation = {'reservation_id': secrets.token_hex(16), 'context_ref': context_ref,
        'effect_id': context['effect']['effect_id'], 'state': 'reserved', 'reserved_at': now['now_utc'],
        'expires_at': proposal.deadline.expires_at, 'expires_monotonic_ns': proposal.deadline.expires_monotonic_ns,
        'expected_state_revision': proposal.observed_state_revision+1}
    reservation_ref = history.retain(canonical_bytes(reservation))
    attempt = {'identity': context['identity'], 'origin_request_ref':
        context['request_ref'] if lineage is None else lineage['event']['origin_request_ref'],
        'request_ref': context['request_ref'], 'context_ref': first_context_ref, 'reservation_ref': reservation_ref,
        'effect': context['effect'], 'predecessor_result_refs':
        [] if lineage is None else lineage['predecessor_result_refs'], 'dispatch_state': 'not_consumed',
        'result_ref': None, 'reconciliation_ref': None, 'consumption_sequence': None, 'consume_clock_ref': None}
    if sql_mandate_raw is not None:
        attempt.update(sql_mandate_ref=history.retain(sql_mandate_raw),sql_xact_ref=None,sql_postimage_ref=None)
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=preimages_ref, request_ref=context['request_ref'], release_ref=release_ref,
        expected_ref=current_ref, phase=('create_intent' if operation == 'install' else
            'purge_intent' if operation == 'purge' else 'reverse_intent'), recorded_at=now['now_utc'],
        deadline=deadline,expected_context=thaw(proposal.context))


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


def _consume(history, reserved_ref, current, clock_raw, *, originals, inputs=None, deadline=None):
    event, obs = _load_entry(history, reserved_ref)
    latest_ref, _, latest = _latest_attempt(history, event['identity'])
    reservation = _decode(history.read(latest['reservation_ref']))
    _require(reservation['state'] == 'reserved', 'reservation_used')
    _require(latest_ref == reserved_ref, 'reservation_invalidated')
    _require(current.observed_state_revision == reservation['expected_state_revision'], 'generation_changed')
    _require(current.observed_state_revision < owner._INT_MAX, 'generation_changed')
    original = _decode(history.read(obs['context_ref']))
    context = thaw(current.context)
    attempt = _decode(history.read(obs['attempt_ref']))
    if attempt.get('sql_mandate_ref') is not None:
        _, admitted = _admitted_deadline(deadline, expected_context=context)
        owner._same_context(current,admitted)
        _require(current.deadline == deadline and deadline.expires_at == reservation['expires_at']
            and deadline.expires_monotonic_ns == reservation['expires_monotonic_ns'],'reservation_expired')
        _sql_mandate(history,history.read(attempt['sql_mandate_ref']),context)
    _continuity(original, context)
    now = owner._decode(clock_raw, 'time_unavailable')
    owner._clock(now, original['owner_epoch'])
    _before_deadline(now, min(reservation['expires_at'], current.deadline.expires_at),
                     min(reservation['expires_monotonic_ns'], current.deadline.expires_monotonic_ns))
    _retain_originals(history, originals, context)
    request_raw = history.read(event['request_ref'])
    if thaw(parse_install_message(request_raw, expected_kind='request').document)['operation'] != 'install':
        _require(type(inputs) is owner._Inputs and inputs.originals == originals
                 and inputs.current_clock_raw == clock_raw, 'owner_unavailable')
        _part1_plan(history, request_raw, current, inputs)
    context['clock_ref'] = history.retain(clock_raw)
    attempt = _decode(history.read(obs['attempt_ref']))
    reservation['state'] = 'consumed'
    reservation_ref = history.retain(canonical_bytes(reservation))
    attempt.update(reservation_ref=reservation_ref, dispatch_state='consumed_result_unknown',
                   consumption_sequence=event['sequence']+1, consume_clock_ref=history.retain(clock_raw))
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=obs['preimages_ref'], request_ref=event['request_ref'],
        release_ref=event['release_binding_ref'], expected_ref=reserved_ref,
        phase=event['phase'], recorded_at=now['now_utc'],deadline=deadline,
        expected_context=thaw(current.context))


def _record_result(history, consumed_ref, original_measurement, *, current_context=None,
                   originals=None, clock_raw=None, result_originals=None, cleanup_plan=None, publication_head=None, publication_revision=None,
                   deadline=None, publication_context=None):
    event, obs = _load_entry(history, consumed_ref)
    anchor_attempt = _decode(history.read(obs['attempt_ref']))
    mandated = anchor_attempt.get('sql_mandate_ref') is not None
    latest_ref, _, latest = _latest_attempt(history, event['identity'])
    if mandated or publication_context is not None:
        admitted_inputs, fresh = _admitted_deadline(deadline, expected_context=publication_context)
        if publication_context is not None:
            if fresh.context['request_ref'] == event['request_ref']:
                _continuity(_decode(history.read(obs['context_ref'])),thaw(fresh.context))
            else:
                recovery_request = thaw(parse_install_message(
                    admitted_inputs.originals[fresh.context['request_ref']['path']],
                    expected_kind='request').document)
                _require(recovery_request['operation'] == 'recover'
                    and recovery_request['identity']['parent_attempt_id'] == event['identity']['attempt_id'],
                    'scope_denied')
                root, root_obs = _load_entry(history,recovery_request['journal_ref'])
                root_attempt = _decode(history.read(root_obs['attempt_ref']))
                _require(all(root[k] == event[k] for k in
                    ('identity','effect','request_ref','origin_request_ref','release_binding_ref'))
                    and root_obs['preimages_ref'] == obs['preimages_ref']
                    and all(root_attempt[k] == anchor_attempt[k] for k in
                        ('consumption_sequence','consume_clock_ref','context_ref'))
                    and root_attempt['consumption_sequence'] is not None
                    and root_attempt['consume_clock_ref'] is not None, 'journal_invalid')
                if mandated:
                    _require(recovery_request['journal_ref'] == consumed_ref,'scope_denied')
    if mandated:
        head = consumed_ref if publication_head is None else publication_head
        prefix = _sql_prefix(history,consumed_ref,head)
        _require(fresh.observed_state_revision == history.current_revision()
            and publication_revision in (None,fresh.observed_state_revision),'generation_changed')
        if prefix['attempt']['dispatch_state'] != 'consumed_result_unknown':
            _require(history.read(prefix['event']['result_ref']) == original_measurement,'resource_changed')
            return prefix['ref']
        if publication_context is None:
            # A normal publisher binds the original scope. Recovery supplies its
            # independently checked fresh context; neither is a bearer permit.
            _continuity(current_context or _decode(history.read(obs['context_ref'])),
                        thaw(fresh.context))
            publication_context = thaw(fresh.context)
        _require(prefix['attempt']['dispatch_state'] == 'consumed_result_unknown','reservation_used')
        obs = _load_entry(history,prefix['ref'])[1]
        publication_head,publication_revision = head,fresh.observed_state_revision
    else:
        _require(latest_ref == consumed_ref, 'reservation_used')
    _require((publication_head is None) == (publication_revision is None),'journal_invalid')
    expected_head = consumed_ref if publication_head is None else publication_head
    if publication_revision is not None:
        _require(type(publication_revision) is int and publication_revision < owner._INT_MAX
            and history.current_revision() == publication_revision,'generation_changed')
    _require(history.head()[0] == expected_head,'generation_changed')
    reservation = _decode(history.read(obs['reservation_ref']))
    attempt = _decode(history.read(obs['attempt_ref']))
    _require(reservation['state'] == 'consumed' and attempt['dispatch_state'] == 'consumed_result_unknown', 'reservation_used')
    result = _decode(original_measurement)
    _require(set(result) == {'identity','effect','request_ref','state','observed'}, 'journal_invalid')
    _require(result['identity'] == event['identity'] and result['effect'] == event['effect']
             and result['request_ref'] == event['request_ref'], 'resource_changed')
    _require(result['state'] in ('applied_verified','not_applied_verified') and type(result['observed']) is dict,
             'attempt_ambiguous')
    if mandated:
        _require(result['observed'].get('sql_checkpoint_head') == expected_head
            and (result['state'] != 'applied_verified' or attempt.get('sql_postimage_ref') is not None),
            'journal_invalid')
    if publication_revision is not None:
        reservation['expected_state_revision'] = publication_revision
        attempt['reservation_ref'] = history.retain(canonical_bytes(reservation))
    result_ref = history.retain(original_measurement)
    attempt.update(dispatch_state=result['state'], result_ref=result_ref)
    if (mandated or publication_context is not None) and result['state'] == 'not_applied_verified':
        attempt['reconciliation_ref'] = result_ref
    context = _decode(history.read(obs['context_ref']))
    if publication_revision is not None:
        context['state_revision'] = publication_revision
    if current_context is not None:
        _continuity(context, current_context)
        context = current_context
        _retain_originals(history, originals, context)
    clock_raw = history.read(attempt['consume_clock_ref']) if clock_raw is None else clock_raw
    clock = owner._decode(clock_raw, 'time_unavailable')
    owner._clock(clock, context['owner_epoch'])
    if result_originals is not None:
        _retain_originals(history, result_originals, {})
    operation = thaw(parse_install_message(history.read(event['request_ref']), expected_kind='request').document)['operation']
    if operation != 'install':
        from . import recovery, uninstall
        request = thaw(parse_install_message(history.read(event['request_ref']), expected_kind='request').document)
        bound_plan = uninstall._plan(history, request, context, recovery._applied_lineage(history, request))
        _require(cleanup_plan == bound_plan, 'resource_changed')
        uninstall._verify_result(history, bound_plan, original_measurement)
    history.read(clock['clock_receipt_ref'])
    context['clock_ref'] = history.retain(clock_raw)
    return _publish_entry(history, context=context, reservation=reservation, attempt=attempt,
        preimages_ref=obs['preimages_ref'], request_ref=event['request_ref'],
        release_ref=event['release_binding_ref'], expected_ref=expected_head,
        phase=('created' if operation == 'install' else 'finished') if result['state'] == 'applied_verified' else 'held',
        recorded_at=clock['now_utc'], result_ref=result_ref,deadline=deadline,
        expected_context=publication_context)


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
    try:
        return _execute_bound(original_request, first)
    except owner.OwnerRefusal:
        raise
    except Exception:
        raise owner.OwnerRefusal('backend_unavailable') from None


def _owner_inputs():
    try:
        return owner._installed_owner_inputs()
    except owner.OwnerRefusal:
        raise
    except Exception:
        raise owner.OwnerRefusal('owner_unavailable') from None


def _current_owner(original_request, deadline):
    inputs = _owner_inputs()
    try:
        return inputs, owner._evaluate(original_request, inputs, previous_deadline=deadline)
    except owner.OwnerRefusal:
        raise
    except Exception:
        raise owner.OwnerRefusal('owner_unavailable') from None


def _part1_plan(history, original_request, current, inputs):
    from . import recovery, uninstall
    request = thaw(parse_install_message(original_request, expected_kind='request').document)
    lineage = recovery._applied_lineage(history, request)
    return lineage, uninstall._current_plan(history, request, current, inputs, lineage)


def _execute_bound(original_request, first):
    bindings = _installed_lifecycle()
    _require(type(bindings) is _Bindings and type(bindings.history) is _History
             and type(bindings.primitives) is dict, 'backend_unavailable')
    history = bindings.history
    primitive = bindings.primitives.get(first.context['effect']['resource_kind'])
    request = thaw(parse_install_message(original_request,expected_kind='request').document)
    sql_mandate_raw = getattr(primitive,'sql_mandate_raw',None)
    mandated = sql_mandate_raw is not None
    if request['operation'] == 'recover':
        _, subject = _load_entry(history,request['journal_ref'])
        mandated = _decode(history.read(subject['attempt_ref'])).get('sql_mandate_ref') is not None
    deadline = first.deadline
    with history.operation(deadline=deadline, expected_context=thaw(first.context)):
        inputs, current = _current_owner(original_request, first.deadline)
        owner._same_context(first, current)
        request = thaw(parse_install_message(original_request,expected_kind='request').document)
        if request['operation'] == 'recover':
            from . import recovery
            return recovery._execute_recovery(history,original_request,current,bindings.primitives)
        primitive = bindings.primitives.get(current.context['effect']['resource_kind'])
        _require(primitive is not None, 'backend_unavailable')
        preimages = primitive.snapshot()
        inputs, refreshed = _current_owner(original_request, current.deadline)
        owner._same_context(current, refreshed)
        reserved = _reserve(history, original_request, refreshed, inputs.originals, preimages,
                            clock_raw=inputs.current_clock_raw, inputs=inputs,sql_mandate_raw=sql_mandate_raw,deadline=deadline)
        inputs, final = _current_owner(original_request, refreshed.deadline)
        consumed = _consume(history, reserved, final, inputs.current_clock_raw,
                            originals=inputs.originals, inputs=inputs,deadline=deadline)
        _, observation = _load_entry(history, consumed)
        # Consume persistence can block. Recheck the common owner at the actual
        # dispatch point; the consumed marker alone cannot authorize dispatch.
        dispatch_inputs, dispatch_current = _current_owner(original_request, final.deadline)
        _require(dispatch_current.observed_state_revision == final.observed_state_revision+1, 'generation_changed')
        _continuity(thaw(final.context), thaw(dispatch_current.context))
        cleanup = None
        request = thaw(parse_install_message(original_request, expected_kind='request').document)
        if request['operation'] != 'install':
            _, cleanup = _part1_plan(history, original_request, dispatch_current, dispatch_inputs)
            # The lineage audit reads durable originals and may block. Reobserve
            # the same owner/context and both cleanup bounds at actual dispatch.
            from . import uninstall
            fresh_inputs, fresh = _current_owner(original_request, dispatch_current.deadline)
            owner._same_context(dispatch_current, fresh)
            uninstall._current_bounds(fresh, fresh_inputs)
            dispatch_inputs, dispatch_current = fresh_inputs, fresh
        effect_data = {'context': thaw(dispatch_current.context), 'journal_ref': consumed,
                       'preimages_ref': observation['preimages_ref']}
        if cleanup is not None:
            effect_data['cleanup'] = cleanup
        effect_data['deadline'] = deadline
        if mandated:
            _sql_prefix(history,consumed,consumed)
        effect_context = freeze(effect_data)
        try:
            # Dispatch preparation can wait after its last owner observation.
            history._check_publication(consumed)
            if cleanup is None or cleanup['targets']:
                primitive.dispatch(effect_context)
            with _measurement(primitive,effect_context,mandated=mandated) as measured:
                # The same measure owns v2 SQL exclusion until durable P1 ACK.
                history.retain(measured)
                result_inputs = _owner_inputs()
                _, fresh = _current_owner(original_request, deadline)
                _continuity(thaw(dispatch_current.context), thaw(fresh.context))
                publication = {'publication_head': history.head()[0],
                    'publication_revision': fresh.observed_state_revision, 'deadline': deadline,
                    'publication_context': thaw(fresh.context)}
                completed = _record_result(history, consumed, measured,
                    current_context=thaw(dispatch_current.context), originals=dispatch_inputs.originals,
                    clock_raw=result_inputs.current_clock_raw, result_originals=result_inputs.originals,
                    cleanup_plan=cleanup,**publication)
                _, after = _current_owner(original_request, deadline)
                _continuity(thaw(fresh.context), thaw(after.context))
                _require(after.observed_state_revision == fresh.observed_state_revision+1, 'generation_changed')
        except owner.OwnerRefusal:
            raise
        except Exception:
            raise owner.OwnerRefusal('attempt_ambiguous') from None
        # Return only the existing immutable P1 event reference. It is neither a
        # client receipt nor a bearer permit; the owner retains its original facts.
        return freeze(completed)
