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
                 module_id, sink, *, manifest, feed=None, after_checkpoint=None):
        try:
            parsed = ModuleManifest.parse(manifest)
        except ValueError:
            raise ConsumerError('invalid_manifest') from None
        if parsed.module_id != module_id or not isinstance(installation_id, UUID) or not isinstance(project_id, UUID):
            raise ConsumerError('invalid_manifest')
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id
        self.module_id = module_id
        self.manifest = parsed
        self.sink = sink
        self.feed = feed or Outbox(connection, credential, installation_id, project_id)
        self.after_checkpoint = after_checkpoint

    def _call(self, signature, args=()):
        try:
            with authorized(self.connection, self.credential,
                            self.installation_id, self.project_id, 'control'):
                result = self.connection.execute(
                    f'SELECT coordination.{signature}', args).fetchone()[0]
            return result
        except (AuthError, psycopg.Error) as error:
            raise ConsumerError('core_unavailable') from error

    def status(self):
        return self._call('c07_status(%s)', (self.module_id,))

    def register(self, *, snapshot_cursor=0, generation=1):
        if type(snapshot_cursor) is not int or type(generation) is not int:
            raise ConsumerError('invalid_input')
        return self._call('c07_register(%s,%s,%s,%s)',
                          (self.module_id, snapshot_cursor, generation, False))

    def begin_rebuild(self, *, snapshot_cursor, generation):
        if type(snapshot_cursor) is not int or type(generation) is not int:
            raise ConsumerError('invalid_input')
        return self._call('c07_register(%s,%s,%s,%s)',
                          (self.module_id, snapshot_cursor, generation, True))

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
            return
        if not repairing and self._call('c07_poisoned(%s,%s,%s)',
                (self.module_id, state['generation'], aggregate)):
            self._record(state, event, 'held')
            return
        try:
            receipt = self.sink.apply(event, state['generation'])
        except ValueError as error:
            if str(error) != 'target_conflict':
                raise ConsumerError('target_unavailable') from error
            self._record(state, event, 'poison', error='target_conflict')
            return
        except Exception as error:
            raise ConsumerError('target_unavailable') from error
        if not isinstance(receipt, dict) or receipt.get('event_id') != env['event_id']:
            raise ConsumerError('invalid_target_receipt')
        outcome = 'stale' if receipt.get('stale') is True else 'applied'
        self._record(state, event, outcome, receipt=receipt)
        if self.after_checkpoint is not None:
            try:
                self.after_checkpoint()
            except Exception as error:
                raise ConsumerError('after_checkpoint') from error

    def cycle(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ConsumerError('invalid_input')
        with self.hold_lock():
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
                self._apply_event(state, event)
            cursor = page.events[-1].cursor if len(page.events) == limit else page.head
            self._call('c07_scan(%s,%s,%s)', (self.module_id, state['generation'], cursor))
            return self.status()

    def repair(self, event_id):
        if not isinstance(event_id, UUID):
            raise ConsumerError('invalid_input')
        with self.hold_lock():
            state = self._state()
            try:
                page = self.feed.page(after=state['applied_cursor'], limit=1000)
            except OutboxError as error:
                raise ConsumerError(error.code) from error
            if not any(UUID(event.envelope['event_id']) == event_id for event in page.events):
                raise ConsumerError('repair_event_missing')
            for event in page.events:
                self._apply_event(state, event, repairing=True)
            return self.status()
