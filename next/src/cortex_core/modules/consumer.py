"""C07 project-scoped durable module consumer.

The target owns revision/tombstone idempotency. Core accepts only a receipt from
that committed target effect and keeps scan progress separate from completeness.
"""
from contextlib import contextmanager
import hashlib
import json
import re
from uuid import UUID

import psycopg

from cortex_core.auth import AuthError, authorized
from cortex_core.outbox import Outbox, OutboxError
from .protocol import ModuleManifest


class ConsumerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ModuleConsumer:
    def __init__(self, connection, credential, installation_id, project_id,
                 module_id, sink, *, manifest, feed=None, after_checkpoint=None,
                 max_pending_outcomes=10000, external_binding=None):
        try:
            parsed = ModuleManifest.parse(manifest)
        except ValueError:
            raise ConsumerError('invalid_manifest') from None
        if parsed.module_id != module_id or not isinstance(installation_id, UUID) or not isinstance(project_id, UUID):
            raise ConsumerError('invalid_manifest')
        if type(max_pending_outcomes) is not int or not 1 <= max_pending_outcomes <= 100000:
            raise ConsumerError('invalid_input')
        if parsed.protocol_version == 2:
            from .vector.projection import ProjectionBinding
            if (type(external_binding) is not ProjectionBinding
                    or external_binding.installation_id != installation_id
                    or external_binding.project_id != project_id or external_binding.module_id != module_id):
                raise ConsumerError('invalid_manifest')
        elif external_binding is not None:
            raise ConsumerError('invalid_manifest')
        self.external_binding = external_binding
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id
        self.module_id = module_id
        self.manifest = parsed
        self.sink = sink
        self.feed = feed or Outbox(connection, credential, installation_id, project_id)
        self.after_checkpoint = after_checkpoint
        self.max_pending_outcomes = max_pending_outcomes

    def _call(self, signature, args=()):
        try:
            with authorized(self.connection, self.credential,
                            self.installation_id, self.project_id, 'control'):
                if signature == 'c07_status(%s)':
                    row = self.connection.execute('SELECT coordination.c07_status(%s)', args)
                elif signature == 'c07_register(%s,%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_register(%s,%s,%s,%s)', args)
                elif signature == 'c09_bind(%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c09_bind(%s,%s,%s::jsonb)', args)
                elif signature == 'c07_expire(%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_expire(%s,%s)', args)
                elif signature == 'c07_poisoned(%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_poisoned(%s,%s,%s)', args)
                elif signature == 'c07_quarantined(%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_quarantined(%s,%s,%s)', args)
                elif signature == 'c07_record(%s,%s,%s,%s,%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_record(%s,%s,%s,%s,%s,%s,%s::jsonb)', args)
                elif signature == 'c07_scan(%s,%s,%s)':
                    row = self.connection.execute('SELECT coordination.c07_scan(%s,%s,%s)', args)
                else:
                    raise ConsumerError('invalid_call')
                result = row.fetchone()[0]
            return result
        except (AuthError, psycopg.Error) as error:
            raise ConsumerError('core_unavailable') from error

    def status(self):
        return self._call('c07_status(%s)', (self.module_id,))

    def register(self, *, snapshot_cursor=0, generation=1):
        if type(snapshot_cursor) is not int or type(generation) is not int:
            raise ConsumerError('invalid_input')
        state = self._call('c07_register(%s,%s,%s,%s)',
                           (self.module_id, snapshot_cursor, generation, False))
        return self._bind_external(state)

    def begin_rebuild(self, *, snapshot_cursor, generation):
        if type(snapshot_cursor) is not int or type(generation) is not int:
            raise ConsumerError('invalid_input')
        state = self._call('c07_register(%s,%s,%s,%s)',
                           (self.module_id, snapshot_cursor, generation, True))
        return self._bind_external(state)

    def _bind_external(self, state):
        if self.external_binding is not None:
            from .vector.projection import binding_contract
            if state['generation'] != self.external_binding.consumer_generation:
                raise ConsumerError('invalid_manifest')
            self._call('c09_bind(%s,%s,%s)', (self.module_id, state['generation'],
                json.dumps(binding_contract(self.external_binding))))
        return state

    @contextmanager
    def hold_lock(self):
        key = f'c07:{self.installation_id}:{self.project_id}:{self.module_id}'
        try:
            got = self.connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                                          (key,)).fetchone()[0]
            if not got:
                raise ConsumerError('busy')
            try:
                yield
            finally:
                self.connection.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',
                                        (key,))
        except psycopg.Error as error:
            raise ConsumerError('core_unavailable') from error

    def _state(self):
        state = self.status()
        if state is None or state['state'] != 'active':
            raise ConsumerError('expired_or_unregistered')
        if state['applied_cursor'] < state['floor']:
            self._call('c07_expire(%s,%s)', (self.module_id, state['generation']))
            raise ConsumerError('expired')
        return state

    def _record(self, state, event, outcome, error=None, receipt=None):
        return self._call('c07_record(%s,%s,%s,%s,%s,%s,%s)',
            (self.module_id, state['generation'], event.cursor,
             UUID(event.envelope['event_id']), outcome, error,
             json.dumps(receipt) if receipt is not None else None))

    def _validate(self, event):
        env = event.envelope
        try:
            if (not isinstance(env, dict) or UUID(env['installation_id']) != self.installation_id
                    or UUID(env['project_id']) != self.project_id
                    or not isinstance(env['aggregate_kind'], str)
                    or UUID(env['aggregate_id']).int == 0
                    or UUID(env['event_id']).int == 0):
                raise ConsumerError('untrusted_scope')
            if (env['schema_version'] != self.manifest.schema_version
                    or env['aggregate_kind'] not in self.manifest.aggregate_kinds
                    or env['aggregate_revision'] < 1
                    or env['operation'] != ('delete' if env['tombstone'] else 'upsert')):
                return 'invalid_event'
            if (not re.fullmatch('[0-9a-f]{64}', env['payload_sha256'])
                    or hashlib.sha256(event.payload).hexdigest() != env['payload_sha256']):
                return 'invalid_digest'
        except (ValueError, KeyError, TypeError):
            raise ConsumerError('untrusted_scope') from None
        return None

    def _apply_event(self, state, event, *, repairing=False):
        error = self._validate(event)
        env = event.envelope
        aggregate = UUID(env['aggregate_id'])
        if error:
            if repairing:
                raise ConsumerError('poison_still_present')
            self._record(state, event, 'poison', error=error)
            return False
        if not repairing and self._call('c07_poisoned(%s,%s,%s)',
                (self.module_id, state['generation'], aggregate)):
            self._record(state, event, 'held')
            return False
        try:
            receipt = self.sink.apply(event, state['generation'])
        except ValueError as error:
            if self.external_binding is not None:
                raise ConsumerError('capability_unavailable') from None
            if str(error) != 'target_conflict':
                raise ConsumerError('target_unavailable') from error
            self._record(state, event, 'poison', error='target_conflict')
            return False
        except Exception as error:
            if self.external_binding is not None:
                raise ConsumerError('capability_unavailable') from None
            raise ConsumerError('target_unavailable') from error
        external_receipt = receipt
        if self.external_binding is not None:
            from .vector.projection import validate_receipt
            try:
                if state['generation'] != self.external_binding.consumer_generation:
                    raise ValueError()
                validate_receipt(receipt, self.external_binding, event)
                receipt = {k:receipt[k] for k in ('event_id','aggregate_id','revision',
                    'tombstone','payload_sha256','generation','stale')}
            except (ValueError, KeyError, TypeError):
                raise ConsumerError('invalid_target_receipt') from None
        required = {'event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale'}
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ConsumerError('invalid_target_receipt')
        try:
            if (not isinstance(receipt['event_id'], str) or UUID(receipt['event_id']) != UUID(env['event_id'])
                    or not isinstance(receipt['aggregate_id'], str) or UUID(receipt['aggregate_id']) != aggregate
                    or type(receipt['revision']) not in (int, float)
                    or not 1 <= receipt['revision'] <= (1 << 63)-1
                    or int(receipt['revision']) != receipt['revision']
                    or receipt['revision'] != env['aggregate_revision']
                    or type(receipt['generation']) not in (int, float)
                    or not 1 <= receipt['generation'] <= (1 << 63)-1
                    or int(receipt['generation']) != receipt['generation']
                    or receipt['generation'] != state['generation']
                    or type(receipt['tombstone']) is not bool or receipt['tombstone'] != env['tombstone']
                    or type(receipt['stale']) is not bool
                    or not isinstance(receipt['payload_sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', receipt['payload_sha256'])
                    or receipt['payload_sha256'] != env['payload_sha256']):
                raise ConsumerError('invalid_target_receipt')
        except (ValueError, TypeError, OverflowError):
            raise ConsumerError('invalid_target_receipt') from None
        outcome = 'stale' if receipt.get('stale') is True else 'applied'
        self._record(state, event, outcome, receipt=external_receipt)
        if self.after_checkpoint is not None:
            try:
                self.after_checkpoint()
            except Exception as error:
                raise ConsumerError('after_checkpoint') from error
        return True

    def cycle(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ConsumerError('invalid_input')
        with self.hold_lock():
            state = self._state()
            # Sparse outcomes and the applied/scan gap are durable work. Replay
            # that observed range before taking another page of new history;
            # no repair-local continuation or cursor reset is needed.
            replay_cursor = state['applied_cursor']
            observed_fence = state['scan_cursor']
            while replay_cursor < observed_fence:
                try:
                    observed = self.feed.page(after=replay_cursor, limit=limit)
                except OutboxError as error:
                    if error.code == 'expired':
                        self._call('c07_expire(%s,%s)', (self.module_id, state['generation']))
                    raise ConsumerError(error.code) from error
                if observed.floor > state['applied_cursor']:
                    self._call('c07_expire(%s,%s)', (self.module_id, state['generation']))
                    raise ConsumerError('expired')
                previous = replay_cursor
                for event in observed.events:
                    if event.cursor > observed_fence:
                        break
                    if event.cursor <= replay_cursor:
                        raise ConsumerError('invalid_replay_page')
                    replay_cursor = event.cursor
                    self._validate(event)
                    aggregate = UUID(event.envelope['aggregate_id'])
                    # Preserve unresolved poison and its held successors. A
                    # resolved root leaves its durable held work eligible.
                    if self._call('c07_poisoned(%s,%s,%s)',
                            (self.module_id, state['generation'], aggregate)):
                        continue
                    self._apply_event(state, event)
                # A scoped feed can exhaust before the global fence because
                # intervening cursors belong to other projects. Stored local
                # completeness stays unchanged; ordinary polling handles new work.
                if replay_cursor == previous:
                    break
            state = self._state()
            try:
                page = self.feed.page(after=state['scan_cursor'], limit=limit)
            except OutboxError as error:
                if error.code == 'expired':
                    self._call('c07_expire(%s,%s)', (self.module_id, state['generation']))
                raise ConsumerError(error.code) from error
            if page.floor > state['applied_cursor']:
                self._call('c07_expire(%s,%s)', (self.module_id, state['generation']))
                raise ConsumerError('expired')
            for event in page.events:
                if self.status()['pending_outcomes'] >= self.max_pending_outcomes:
                    raise ConsumerError('capacity')
                self._apply_event(state, event)
            cursor = page.events[-1].cursor if len(page.events) == limit else page.head
            self._call('c07_scan(%s,%s,%s)', (self.module_id, state['generation'], cursor))
            return self.status()

    def repair(self, event_id):
        if not isinstance(event_id, UUID):
            raise ConsumerError('invalid_input')
        with self.hold_lock():
            state = self._state()
            if not self._call('c07_quarantined(%s,%s,%s)', (self.module_id, state['generation'], event_id)):
                raise ConsumerError('repair_event_not_poison')
            # Admission is captured once while locked: successful root apply
            # resolves its quarantine, but does not invalidate continuation.
            cursor = state['applied_cursor']
            scan_fence = state['scan_cursor']
            requested = None
            aggregate = None
            while cursor < scan_fence:
                try:
                    page = self.feed.page(after=cursor, limit=1000)
                except OutboxError as error:
                    raise ConsumerError(error.code) from error
                page_cursor = cursor
                for event in page.events:
                    if event.cursor > scan_fence:
                        break
                    if event.cursor <= page_cursor:
                        raise ConsumerError('invalid_repair_page')
                    page_cursor = event.cursor
                    if requested is None:
                        if UUID(event.envelope['event_id']) != event_id:
                            continue
                        requested = event
                        aggregate = UUID(event.envelope['aggregate_id'])
                        if not self._apply_event(state, event, repairing=True):
                            return self.status()
                    elif UUID(event.envelope['aggregate_id']) == aggregate:
                        if not self._apply_event(state, event):
                            return self.status()
                if page_cursor == cursor:
                    break
                cursor = page_cursor
            if requested is None:
                raise ConsumerError('repair_event_missing')
            return self.status()
