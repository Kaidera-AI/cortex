"""S2 bounded private HTTP probe. Native custody/placement and READY are UNBOUND.

Only an explicit controlled MockTransport is admitted in this source profile.
No default socket transport, Core binding, apply receipt or engine fallback.
"""
import asyncio
import copy
import json
import math
import re
import time
from uuid import uuid4

import httpx

from cortex_core.runtime.contracts import decode
from .qdrant import (
    InertPort, MAX_RESPONSE_BYTES, ProtocolFailure, admit_descriptor, admit_private_config,
)

MAX_IN_FLIGHT = 8
MAX_PENDING = 16  # Bound owned work, including requests queued behind the eight slots.
_COLLECTION = r'/collections/c09-probe-[0-9a-f]{32}'


def _check(deadline):
    # Also catch synchronous dependencies which prevent asyncio's timer running.
    if time.monotonic() >= deadline:
        raise TimeoutError()


class PrivateClient:
    def __init__(self, descriptor, platform, config, *, resolve_key, transport=None,
                 timeout_seconds=5):
        try:
            self._descriptor = admit_descriptor(descriptor, platform)
            self._config = admit_private_config(config)
            if (not callable(resolve_key) or type(timeout_seconds) not in (int, float)
                    or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5):
                raise ValueError()
        except Exception:
            raise ProtocolFailure('qdrant_admission_refused') from None
        self._resolve_key, self._transport = resolve_key, transport
        self._budget = timeout_seconds
        host = self._config['service']['host']
        self._origin = f'http://[{host}]:6333' if ':' in host else f'http://{host}:6333'
        self._http = None
        self._closed = False
        self._refused = False
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._jobs = set()
        self._closer = None

    def status(self):
        return {'state': 'unavailable', 'module_ready': False, 'core_auth_bound': False,
                'native_qualified': False, 'transport_profile': 'S2_CONTROLLED_FAKE',
                'reason': 'native_binding_unavailable', 'pending_operations': len(self._jobs)}

    async def __aenter__(self):
        # RFC1918/ULA metadata alone cannot prove same-host placement. Do not invent
        # another custody owner or accept a caller's native/READY boolean instead.
        if (self._closed or self._http is not None
                or type(self._transport) is not httpx.MockTransport):
            raise ProtocolFailure('qdrant_native_binding_unavailable')
        self._http = httpx.AsyncClient(
            transport=self._transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(self._budget, connect=min(2, self._budget)),
            limits=httpx.Limits(max_connections=MAX_IN_FLIGHT,
                                max_keepalive_connections=MAX_IN_FLIGHT))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    def _settled(self, task):
        self._jobs.discard(task)
        if not task.cancelled():
            task.exception()  # Observe errors even when the deadline waiter has left.

    async def aclose(self, *, deadline=None):
        self._closed = True
        for task in self._jobs:
            task.cancel()
        if self._http is not None and self._closer is None:
            self._closer = asyncio.create_task(self._http.aclose())
        targets = set(self._jobs)
        if self._closer is not None:
            targets.add(self._closer)
        if not targets:
            return
        end = self._deadline(deadline)
        _, pending = await asyncio.wait(targets, timeout=max(0, end-time.monotonic()))
        if pending or self._closer is not None and (
                self._closer.cancelled() or self._closer.exception() is not None):
            # Pending jobs remain owned and counted; never call them cleaned up.
            raise ProtocolFailure('qdrant_cleanup_unverified', cleanup_reason='qdrant_cleanup_unverified')

    def _deadline(self, deadline):
        now = time.monotonic()
        if deadline is None:
            return now + self._budget
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ProtocolFailure('qdrant_deadline_refused')
        return min(deadline, now + self._budget)

    async def probe(self, *, deadline=None):
        return await self._owned(self._probe, deadline)

    async def _owned(self, operation, deadline):
        if self._closed or self._http is None or self._refused or len(self._jobs) >= MAX_PENDING:
            raise ProtocolFailure('qdrant_unavailable')
        end = self._deadline(deadline)
        if end <= time.monotonic():
            raise ProtocolFailure('qdrant_timeout')
        task = asyncio.create_task(operation(end))
        self._jobs.add(task)
        task.add_done_callback(self._settled)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, end-time.monotonic()))
            if not done:
                self._refused = True
                task.cancel()
                raise ProtocolFailure('qdrant_timeout')
            return await task
        except asyncio.CancelledError:
            self._refused = True
            task.cancel()
            if asyncio.current_task().cancelling():
                raise
            raise ProtocolFailure('qdrant_unavailable') from None

    async def _request(self, method, path, body, key, deadline, *, expected_status=200, collection=None):
        try:
            _check(deadline)
            route = ((method == 'GET' and path == '/')
                     or method in ('PUT', 'DELETE') and re.fullmatch(_COLLECTION, path)
                     or method in ('PUT', 'POST') and re.fullmatch(_COLLECTION + '/points', path))
            if collection is not None:
                route = (re.fullmatch(r'/collections/cortex-[0-9a-f]{32}', collection) is not None
                         and path == collection+'/points' and method in ('PUT','POST'))
            if not route or self._http is None or self._closed:
                raise ValueError()
            if method == 'PUT' and path.endswith('/points') and (
                    type(body) is not dict or body.get('update_mode') not in ('insert_only', 'update_only')):
                raise ValueError()  # Never send default/ordinary upsert, even in a probe.
            content = None if body is None else json.dumps(
                body, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if content is not None and len(content) > MAX_RESPONSE_BYTES:
                raise ValueError()
            _check(deadline)
            remaining = deadline-time.monotonic()
            params = {'wait': 'true', 'ordering': 'strong'} if method == 'PUT' and path.endswith('/points') else None
            async with self._http.stream(method, self._origin+path, content=content,
                    params=params, headers={'api-key': key, 'Accept': 'application/json',
                        'Accept-Encoding': 'identity', 'Content-Type': 'application/json'},
                    timeout=httpx.Timeout(remaining, connect=min(2, remaining))) as response:
                _check(deadline)
                if (response.status_code != expected_status
                        or response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
                        or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
                    raise ValueError()
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    _check(deadline)
                    if len(raw)+len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError()
                    raw.extend(chunk)
                value = decode(bytes(raw), limit=MAX_RESPONSE_BYTES, text_payload=True)
                if type(value) is not dict:
                    raise ValueError()
                _check(deadline)
            _check(deadline)
            return value
        except Exception:
            raise ProtocolFailure('qdrant_protocol_refused') from None

    @staticmethod
    def _completed(value):
        result = value.get('result')
        if (value.get('status') != 'ok' or type(result) is not dict
                or type(result.get('operation_id')) is not int or result['operation_id'] < 0
                or result.get('status') != 'completed'):
            raise ProtocolFailure('qdrant_completion_unproved')

    async def _readback(self, collection, point, reader, deadline):
        value = await self._request('POST', collection+'/points',
            {'ids': [1, 2], 'with_vector': True, 'with_payload': True}, reader, deadline)
        rows = value.get('result')
        if (value.get('status') != 'ok' or type(rows) is not list or len(rows) != 1
                or not InertPort._point_matches(rows[0], point)):
            raise ProtocolFailure('qdrant_readback_refused')

    async def _probe(self, deadline):
        async with self._slots:  # Queue time belongs to the SAME total deadline.
            created = False
            primary = None
            writer = reader = None
            collection = '/collections/c09-probe-' + uuid4().hex
            verified = []
            try:
                _check(deadline)
                writer, reader = await self._keys(deadline)
                value = await self._request('GET', '/', None, reader, deadline)
                if value.get('version') != self._descriptor['version']:
                    raise ProtocolFailure('qdrant_version_refused')
                verified.append('version')
                created = True  # A lost acknowledgement can follow a real/fake effect.
                value = await self._request('PUT', collection,
                    {'vectors': {'size': 768, 'distance': 'Dot'}}, writer, deadline)
                if value.get('result') is not True or value.get('status') != 'ok':
                    raise ProtocolFailure('qdrant_collection_refused')
                point = {'id': 1, 'vector': [1.0] + [0.0]*767,
                         'payload': {'probe': collection.rsplit('-', 1)[1], 'revision': 1, 'deleted': False}}
                initial = {'points': [point], 'update_mode': 'insert_only'}
                await self._request('PUT', collection+'/points', initial, reader, deadline, expected_status=403)
                verified.append('reader_denied_write')
                self._completed(await self._request('PUT', collection+'/points', initial, writer, deadline))
                await self._readback(collection, point, reader, deadline)
                verified.append('insert_only')
                newer = copy.deepcopy(point); newer['payload']['revision'] = 2; newer['vector'][0] = .5
                def update(revision, candidate):
                    return {'points': [candidate], 'update_mode': 'update_only', 'update_filter': {'must': [
                        {'key': 'probe', 'match': {'value': point['payload']['probe']}},
                        {'key': 'revision', 'match': {'value': revision}}]}}
                self._completed(await self._request('PUT', collection+'/points', update(999, newer), writer, deadline))
                await self._readback(collection, point, reader, deadline)
                verified.append('filter_miss')
                self._completed(await self._request('PUT', collection+'/points',
                    {'points': [newer], 'update_mode': 'insert_only'}, writer, deadline))
                await self._readback(collection, point, reader, deadline)
                verified.append('existing_insert_only')
                missing = copy.deepcopy(newer); missing['id'] = 2
                self._completed(await self._request('PUT', collection+'/points', update(1, missing), writer, deadline))
                await self._readback(collection, point, reader, deadline)
                verified.append('missing_update_only')
                self._completed(await self._request('PUT', collection+'/points', update(1, newer), writer, deadline))
                await self._readback(collection, newer, reader, deadline)
                verified.append('update_only')
            except asyncio.CancelledError:
                primary = 'qdrant_cancelled'
                raise
            except Exception as error:
                primary = error.reason if isinstance(error, ProtocolFailure) else 'qdrant_unavailable'
                raise ProtocolFailure(primary) from None
            finally:
                try:
                    if created:
                        value = await self._request('DELETE', collection, None, writer, deadline)
                        if value.get('result') is not True or value.get('status') != 'ok':
                            raise ValueError()
                        verified.append('cleanup')
                except Exception:
                    raise ProtocolFailure('qdrant_cleanup_unverified', primary_reason=primary,
                                          cleanup_reason='qdrant_cleanup_unverified') from None
                finally:
                    writer = reader = None
            return {**self.status(), 'verified_operations': verified}


    async def _keys(self, deadline):
        try:
            writer = await self._resolve_key(self._config['secret_refs']['writer'])
            _check(deadline)
            reader = await self._resolve_key(self._config['secret_refs']['reader'])
        except Exception:
            raise ProtocolFailure('qdrant_credentials_refused') from None
        _check(deadline)
        if (any(type(key) is not str or not 1 <= len(key) <= 4096
                or any(not '!' <= ch <= '~' for ch in key) for key in (writer, reader))
                or writer == reader):
            raise ProtocolFailure('qdrant_credentials_refused')
        return writer, reader

    async def apply_projection(self, binding, event, vector, *, deadline=None):
        from .projection import build_point
        try:
            desired = build_point(binding, event, vector)
            return await self._owned(lambda end: self._apply_projection(binding, event, desired, end), deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProtocolFailure('qdrant_projection_unavailable') from None

    async def _projection_read(self, binding, aggregate_id, reader, deadline):
        from .projection import point_key, point_id, validate_point
        identity = point_id(point_key(binding, aggregate_id))
        value = await self._request('POST', binding.collection+'/points',
            {'ids':[identity], 'with_vector':True, 'with_payload':True}, reader, deadline,
            collection=binding.collection)
        rows = value.get('result')
        if value.get('status') != 'ok' or type(rows) is not list or len(rows) > 1:
            raise ProtocolFailure('qdrant_readback_refused')
        return validate_point(rows[0], binding, aggregate_id) if rows else None

    async def _apply_projection(self, binding, event, desired, deadline):
        from uuid import UUID
        from .projection import point_key, make_receipt
        writer = reader = None
        async with self._slots:
            try:
                _check(deadline)
                writer, reader = await self._keys(deadline)
                aggregate = UUID(event.envelope['aggregate_id'])
                observed = await self._projection_read(binding, aggregate, reader, deadline)
                candidate = desired
                if observed is None:
                    body = {'points':[candidate], 'update_mode':'insert_only'}
                else:
                    previous = observed['payload']; revision = desired['payload']['revision']
                    if previous['revision'] == revision and previous['effect_sha256'] != desired['payload']['effect_sha256']:
                        raise ProtocolFailure('qdrant_effect_conflict')
                    if previous['revision'] >= revision:
                        candidate = observed  # Reconcile replay/stale with a conditional complete-point no-op.
                    conditions = point_key(binding, aggregate) | {
                        'revision':previous['revision'], 'effect_sha256':previous['effect_sha256']}
                    body = {'points':[candidate], 'update_mode':'update_only',
                        'update_filter':{'must':[{'key':k,'match':{'value':v}} for k,v in conditions.items()]}}
                completion = await self._request('PUT', binding.collection+'/points', body,
                    writer, deadline, collection=binding.collection)
                self._completed(completion)
                actual = await self._projection_read(binding, aggregate, reader, deadline)
                if actual is None or actual['payload']['revision'] < desired['payload']['revision']:
                    raise ProtocolFailure('qdrant_effect_unproved')
                if (actual['payload']['revision'] == desired['payload']['revision']
                        and actual != desired):
                    raise ProtocolFailure('qdrant_effect_conflict')
                _check(deadline)
                return make_receipt(binding, event, desired, actual, completion['result']['operation_id'])
            finally:
                writer = reader = None
