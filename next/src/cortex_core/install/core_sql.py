"""Private finite S02-S05 composition over the existing P1 phase primitives.

Installed compilation is deliberately absent: X1-c must bind the actual protected
X2/X3/X11 suppliers. Neither a callback, original reference nor these source DATA
checks admits effects. No journal, native backend, credential view or ready bit
is created here. P1 alone consumes, checkpoints, reconciles and publishes.
"""
from contextlib import contextmanager
import time

from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from . import lifecycle, owner

_PHASES = ('init', 'ready', 'bootstrap', 'migrate', 'prepare', 'bind', 'close')
_INVENTORY = frozenset(('sessions', 'recovery', 'carriers', 'children', 'authentication'))


class _CoreSQL:
    """One fixed-owner P2 adapter, not a caller-selectable phase/provider API.

    The inner primitive retains its actual SQL transaction/checkpoint/measurement
    lifetime. `observe` is the independently protected existing owner observation
    seam, never its dispatch return or a self-attested closed flag. Source tests
    bind explicit DATA doubles; the installed lifecycle stays fail-closed.
    """
    def __init__(self, *, history, identity, phase, primitive, observe,
                 selection_ref, windows_ref, previous_refs):
        owner._require(type(history) is lifecycle._History and phase in (*_PHASES, 'ordinary')
                       and callable(observe) and primitive is not observe, 'backend_unavailable')
        self.history, self.identity, self.phase = history, thaw(identity), phase
        self.primitive, self.observe = primitive, observe
        self.selection_ref, self.windows_ref = thaw(selection_ref), thaw(windows_ref)
        self.previous_refs = thaw(previous_refs)
        self.sql_mandate_raw = getattr(primitive, 'sql_mandate_raw', None)
        self._stop = None

    def snapshot(self):
        return self.primitive.snapshot()

    def _guard(self, context, *, recovery=None):
        c = thaw(context['context'])
        event, obs = lifecycle._load_entry(self.history, thaw(context['journal_ref']))
        owner._require(c['identity'] == self.identity and all(event[k] == c[k]
            for k in ('identity', 'effect', 'request_ref')), 'scope_denied')
        attempt = lifecycle._decode(self.history.read(obs['attempt_ref']))
        reservation = lifecycle._decode(self.history.read(obs['reservation_ref']))
        owner._require(reservation['state'] == 'consumed' and attempt['dispatch_state'] == 'consumed_result_unknown',
                       'reservation_used')
        if attempt.get('sql_mandate_ref') is not None:
            lifecycle._sql_prefix(self.history, thaw(context['journal_ref']), self.history.head()[0])
        else:
            owner._require(self.history.head()[0] == thaw(context['journal_ref']), 'reservation_used')
        if recovery is None:
            request = self.history.read(event['request_ref'])
            inputs, fresh = lifecycle._current_owner(request, context.get('deadline'))
            lifecycle._continuity(c, thaw(fresh.context))
        else:
            # The existing recovery gate owns its fresh, distinct authorization.
            owner._require(type(recovery) is owner.OwnerProposal, 'scope_denied')
            inputs = lifecycle._owner_inputs()
            ref = thaw(recovery.context)['request_ref']
            request = inputs.originals[ref['path']]
            fresh = owner._evaluate(request, inputs, previous_deadline=recovery.deadline)
            owner._same_context(recovery, fresh)
        owner._require(fresh.observed_state_revision == self.history.current_revision(), 'generation_changed')
        windows = lifecycle._decode(self.history.read(self.windows_ref))
        owner._require(windows['operation'] == list(lifecycle._operation(self.identity))
            and windows['owner_epoch'] == c['owner_epoch']
            and all(type(windows[k]) is int and windows[k] > 0 for k in ('E', 'init_start'))
            and (windows.get('finite_start') is None or type(windows['finite_start']) is int
                 and windows['finite_start'] >= windows['init_start']),
            'time_unavailable')
        clock = owner._decode(inputs.current_clock_raw, 'time_unavailable')
        deadline = fresh.deadline
        stop = deadline.expires_monotonic_ns
        if recovery is None:
            stop = min(stop, windows['E'])
            if self.phase != 'ready':
                phase_start = windows['init_start'] if self.phase == 'init' else windows['finite_start']
                owner._require(type(phase_start) is int and clock['monotonic_ns'] >= phase_start, 'time_unavailable')
                stop = min(stop, phase_start + 120_000_000_000)
        remaining = min(stop - clock['monotonic_ns'],
            (deadline.expires_at-clock['now_utc']-clock['uncertainty_seconds'])*1_000_000_000)
        owner._require(remaining > 0, 'reservation_expired')
        local_stop = time.monotonic_ns() + remaining
        self._stop = local_stop if self._stop is None else min(self._stop, local_stop)
        owner._require(time.monotonic_ns() < self._stop, 'reservation_expired')
        return c

    def _selection(self):
        selected = lifecycle._decode(self.history.read(self.selection_ref))
        rows = selected.get('migrations')
        owner._require(type(rows) is list and rows and all(type(r) is list and len(r) == 2
            and type(r[0]) is str and lifecycle._DIGEST.fullmatch(r[1]) for r in rows)
            and len({r[0] for r in rows}) == len(rows) and selected.get('through') == rows[-1][0]
            and type(selected.get('manifest_sha256')) is str
            and lifecycle._DIGEST.fullmatch(selected['manifest_sha256']), 'resource_changed')
        return selected

    def _previous(self, context):
        count = len(_PHASES) if self.phase == 'ordinary' else _PHASES.index(self.phase)
        owner._require(type(self.previous_refs) is list and len(self.previous_refs) == count, 'attempt_ambiguous')
        current, _ = lifecycle._load_entry(self.history, thaw(context['journal_ref']))
        sequence = -1
        for phase, reference in zip(_PHASES, self.previous_refs):
            event, _ = lifecycle._load_entry(self.history, reference)
            owner._require(event['phase'] == 'created' and event['result_ref'] is not None
                and sequence < event['sequence'] < current['sequence']
                and lifecycle._operation(event['identity']) == lifecycle._operation(self.identity)
                and event['release_binding_ref'] == current['release_binding_ref'], 'attempt_ambiguous')
            result = lifecycle._decode(self.history.read(event['result_ref']))
            original = result['observed'].get('core_sql', {})
            owner._require(result['state'] == 'applied_verified' and all(result[k] == event[k]
                for k in ('identity', 'effect', 'request_ref')) and original.get('phase') == phase
                and original.get('selection_ref') == self.selection_ref
                and original.get('previous_refs') == self.previous_refs[:_PHASES.index(phase)],
                'attempt_ambiguous')
            old_window = lifecycle._decode(self.history.read(original['windows_ref']))
            current_window = lifecycle._decode(self.history.read(self.windows_ref))
            owner._require(all(old_window[k] == current_window[k] for k in ('operation','owner_epoch','E','init_start'))
                and (phase in ('init','ready') or old_window['finite_start'] == current_window['finite_start']),
                'time_unavailable')
            self.history.read(original['observation_ref'])
            sequence = event['sequence']

    def _observed(self, context, *, post, recovery=None):
        c = self._guard(context, recovery=recovery)
        raw = self.observe(context)
        self._guard(context, recovery=recovery)  # original E/I/M after blocking observation
        value = lifecycle._decode(raw)
        owner._require(value.get('identity') == c['identity'] and value.get('phase') == self.phase
            and value.get('selection_ref') == self.selection_ref and value.get('windows_ref') == self.windows_ref,
            'resource_changed')
        selected = self._selection()
        if self.phase == 'init':
            root = value.get('root', {})
            owner._require(value.get('fresh') is True and all(root.get(k) is True for k in ('held', 'owned'))
                and (post or root.get('empty') is True) and type(root.get('uid')) is int
                and root['uid'] > 0 and root['uid'] == root.get('server_uid'), 'resource_changed')
        if self.phase == 'ready':
            owner._require(value.get('sql_ready') is True and value.get('probe_closed') is True,
                           'backend_unavailable')
        if self.phase in ('migrate', 'prepare', 'bind', 'close'):
            owner._require(value.get('bootstrap_closed') is True and value.get('finite_phase') == 'migrator',
                           'scope_denied')
        if self.phase == 'bootstrap' and post:
            owner._require(value.get('bootstrap_closed') is True, 'attempt_ambiguous')
        if post and self.phase in ('migrate', 'prepare', 'bind', 'close', 'ordinary'):
            owner._require(value.get('committed') is True and value.get('ledger') == selected['migrations'],
                           'attempt_ambiguous')
        if post and self.phase in ('bind', 'close', 'ordinary'):
            owner._require(value.get('roles_bound') is True and value.get('acl_exact') is True, 'resource_changed')
        if post and self.phase in ('close', 'ordinary'):
            unresolved = value.get('unresolved')
            owner._require(value.get('finite_disabled') is True and value.get('auth_fenced') is True
                and type(unresolved) is dict and set(unresolved) == _INVENTORY
                and all(type(v) is list and not v for v in unresolved.values()), 'attempt_ambiguous')
        if self.phase == 'ordinary':
            owner._require(value.get('old_generation_absent') is True and value.get('applied_cleaned') is True,
                           'attempt_ambiguous')
        return raw

    def dispatch(self, context):
        self._guard(context)
        self._previous(context)
        self._observed(context, post=False)
        self._guard(context)
        if self.phase != 'ordinary':
            self.primitive.dispatch(context)
        self._guard(context)

    def _finish(self, context, raw, *, recovery=None):
        c = self._guard(context, recovery=recovery)
        result = lifecycle._decode(raw)
        owner._require(all(result[k] == c[k] for k in ('identity', 'effect', 'request_ref'))
            and result['state'] in ('applied_verified', 'not_applied_verified'), 'attempt_ambiguous')
        observation = self._observed(context, post=result['state'] == 'applied_verified', recovery=recovery)
        if recovery is not None:
            # Never synthesize a fresh terminal result or a retry from new DATA.
            original = result['observed'].get('core_sql', {})
            owner._require(original.get('phase') == self.phase and original.get('selection_ref') == self.selection_ref
                and original.get('windows_ref') == self.windows_ref
                and original.get('previous_refs') == self.previous_refs, 'attempt_ambiguous')
            self.history.read(original['observation_ref'])
            return raw
        original = {'phase':self.phase, 'selection_ref':self.selection_ref, 'windows_ref':self.windows_ref,
            'previous_refs':self.previous_refs, 'observation_ref':self.history.retain(observation)}
        self._guard(context)
        result['observed'] = {**result['observed'], 'core_sql':original}
        return canonical_bytes(result)

    @contextmanager
    def _measured(self, context, *, recovery=None):
        # Preserve the inner SQL exclusion across observation and P1 publication.
        measured = self.primitive.measure(context, **({} if recovery is None else {'recovery':recovery}))
        with measured as answer:
            if recovery is None:
                yield self._finish(context, answer)
            else:
                state, raw = answer
                if state == 'UNKNOWN':
                    yield answer
                else:
                    yield state, self._finish(context, raw, recovery=recovery)

    def measure(self, context, *, recovery=None):
        if self.sql_mandate_raw is not None:
            return self._measured(context, recovery=recovery)
        if recovery is not None:
            state, raw = self.primitive.measure(context, recovery=recovery)
            return (state, raw) if state == 'UNKNOWN' else (state, self._finish(context, raw, recovery=recovery))
        return self._finish(context, self.primitive.measure(context))
