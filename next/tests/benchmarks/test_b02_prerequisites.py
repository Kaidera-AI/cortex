"""Frozen causal controls for Kai's dispatcher/setup/P3 prerequisite ruling."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from vector_baseline import benchmark, corpus, geometry, load, qdrant, report
from test_b02_load import Client
from test_b02_qdrant import Engine
from test_b02_report import fixture


def valid_run():
    run = fixture()
    for row in run['records']:
        row['dispatched_at'] = row['scheduled_at']
        row['dispatcher_lag_seconds'] = 0.0
    return run


def summarize(run):
    return report.summarize(run, latency_budget_ms=100,
                            bindings={'dataset': 'synthetic', 'heldout_query_ids': ['q']})


def make_geometry(root, count=24):
    rows = [{'id': f'{i:064x}', 'project': 'a' * 64, 'type': 'document',
             'month': '2026-10', 'vector': [1, i / 10]} for i in range(count)]
    source = root / 'synthetic.jsonl'
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    c = geometry.prepare(source, root / 'corpus', dimension=2, expected_count=count)
    for index, row in enumerate(rows):
        row['offset'] = index
    return c, rows, np.asarray([row['vector'] for row in rows], dtype='<f4')


def frozen_reference(c, rows, vectors, split):
    # Exact pre-hoist record algorithm; vector witness is independent of production.
    def digest(vector):
        return hashlib.sha256(np.asarray(vector, dtype='<f4').tobytes()).hexdigest()
    sources = {row['id']: digest(vectors[row['offset']]) for row in rows
               if row['id'] in set(split['tuning']) | set(split['heldout'])}
    queries = {}
    for name in ('tuning', 'heldout'):
        path = c.path / (name + '.jsonl')
        values = [json.loads(line) for line in path.read_text().splitlines()]
        ordered = []
        for query in values:
            value = digest(query['vector'])
            if query['source_id'] not in split[name] or sources[query['source_id']] != value:
                raise ValueError('invalid reference source geometry')
            ordered.append({'id': query['id'], 'source_id': query['source_id'], 'vector_sha256': value})
        queries[name] = {'sha256': corpus.digest(path), 'count': len(values), 'ordered': ordered}
    record = {'schema': 'cortex-b02-frozen-input-v1', 'input_sha256': c.manifest['geometry']['input_sha256'],
              'input_count': c.manifest['geometry']['input_count'], 'identity': c.identity,
              'split': split, 'candidate_count': len(c.records), 'candidate_files': c.manifest['files'],
              'manifest_sha256': corpus.digest(c.path / 'manifest.json'),
              'query_manifest_sha256': corpus.digest(c.path / 'query-manifest.json'),
              'source_vectors': sources, 'queries': queries}
    return (json.dumps(record, sort_keys=True, indent=2) + '\n').encode()


class PrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_offer_records_coherent_dispatcher_lag_in_both_phases(self):
        class Clock:
            value = 100.0
            def now(self):
                return self.value
            async def sleep(self, delay):
                self.value += delay
        run = await load.run([{'id': 'q'}], lambda i: Client(i),
                             load.RunConfig(duration_seconds=.2, warmup_seconds=.2), clock=Clock())
        rows = run['records'] + run['warmup_records']
        self.assertEqual(len(rows), 16)
        self.assertTrue(all('dispatcher_lag_seconds' in row for row in rows), 'every offer needs lag provenance')
        for row in rows:
            self.assertAlmostEqual(row['dispatcher_lag_seconds'], row['dispatched_at'] - row['scheduled_at'])

    def test_sent_late_response_is_invalid_harness_not_engine_pass_or_fail(self):
        run = valid_run(); row = run['records'][7]
        row.update(dispatched_at=row['scheduled_at'] + .03, dispatcher_lag_seconds=.03,
                   started_at=row['scheduled_at'] + .03, completed_at=row['scheduled_at'] + .04,
                   latency_seconds=.04, queue_seconds=.03)
        result = summarize(run); d = result['diagnostic']
        self.assertEqual(d['verdict'], 'INVALID_HARNESS')
        self.assertFalse(d['harness_valid'])
        self.assertEqual(d['dispatcher_lag']['measured']['late_offer_ids'], [7])
        self.assertEqual(d['dispatcher_lag']['measured']['count'], 40)
        self.assertAlmostEqual(d['dispatcher_lag']['measured']['max_ms'], 30)
        for field in ('throughput_status', 'latency_status', 'recall_status'):
            self.assertEqual(d[field], 'NOT_RUN')
        self.assertFalse(result['gtm_qualified'])

    def test_dropped_offer_is_invalid_harness_even_with_zero_reported_lag(self):
        run = valid_run(); run['records'][7].update(status='MISSED', started_at=None,
                                                   queue_seconds=None, latency_seconds=10)
        d = summarize(run)['diagnostic']
        self.assertEqual(d['verdict'], 'INVALID_HARNESS')
        self.assertEqual(d['dispatcher_lag']['measured']['dropped_offer_ids'], [7])
        self.assertEqual(d['errors_or_missed'], 1)

    def test_warmup_late_offer_invalidates_the_cell(self):
        run = valid_run(); run['warmup_records'] = copy.deepcopy(run['records'])
        row = run['warmup_records'][3]
        row.update(dispatched_at=row['scheduled_at'] + .03, dispatcher_lag_seconds=.03)
        d = summarize(run)['diagnostic']
        self.assertEqual(d['verdict'], 'INVALID_HARNESS')
        self.assertEqual(d['dispatcher_lag']['warmup']['late_offer_ids'], [3])

    def test_legacy_timestamps_derive_lag_but_missing_or_incoherent_lag_is_invalid(self):
        legacy = valid_run()
        for row in legacy['records']:
            row.pop('dispatcher_lag_seconds')
        self.assertEqual(summarize(legacy)['diagnostic']['verdict'], 'PASS')
        for fault in ('missing', 'incoherent', 'negative', 'bool'):
            with self.subTest(fault=fault):
                run = valid_run(); row = run['records'][2]
                if fault == 'missing':
                    row.pop('dispatcher_lag_seconds'); row.pop('dispatched_at')
                elif fault == 'incoherent':
                    row['dispatcher_lag_seconds'] = .02
                elif fault == 'negative':
                    row['dispatcher_lag_seconds'] = -.01
                else:
                    row['dispatcher_lag_seconds'] = False
                self.assertEqual(summarize(run)['diagnostic']['verdict'], 'INVALID_HARNESS')

    def test_invalid_harness_cli_has_distinct_nonzero_status(self):
        result = {'diagnostic': {'verdict': 'INVALID_HARNESS'}, 'cleanup_verified': True}
        with tempfile.TemporaryDirectory() as tmp, patch.object(benchmark, 'execute', return_value=result), \
                patch('sys.argv', ['benchmark', tmp, str(Path(tmp) / 'result.json')]):
            (Path(tmp) / 'result.json').write_text('{}')
            self.assertEqual(benchmark.main(), 2)

    def test_setup_memory_or_foreign_refusal_records_actual_phase_and_reason(self):
        for free, foreign, reason in [(34, '', 'memory_below_floor'), (35, 'other-test', 'foreign_test_stack')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                engine = Engine(); calls = []
                def gate(runner):
                    calls.append(1)
                    if len(calls) == 2:
                        qdrant.check_preflight(free, foreign)
                stack = qdrant.DisposableQdrant(architecture='arm64', runner=engine, gate=gate,
                                               lock_path=Path(tmp) / 'slot')
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                receipt = getattr(stack, 'failure_receipt', None)
                self.assertIsInstance(receipt, dict, 'refusal cause must survive cleanup')
                self.assertEqual(receipt['primary']['phase'], 'preflight_engine_start')
                self.assertIn(reason, receipt['primary']['admission']['reasons'])
                self.assertEqual(receipt['primary']['admission']['free_percent'], free)
                self.assertFalse(any(cmd[0] == 'start' for cmd, _ in engine.commands))
                self.assertTrue(stack.cleanup_verified)
                self.assertIsNone(stack.password)
                self.assertIsNone(stack.lock)

    def test_failed_effect_primary_cause_survives_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine('container'); engine.fail_remove = True
            stack = qdrant.DisposableQdrant(architecture='arm64', runner=engine, gate=lambda _: None,
                                           lock_path=Path(tmp) / 'slot')
            try:
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                receipt = getattr(stack, 'failure_receipt', None)
                self.assertIsInstance(receipt, dict, 'primary cause must not be replaced by cleanup')
                self.assertEqual(receipt['primary']['phase'], 'create_engine')
                self.assertEqual(receipt['cleanup']['error_class'], 'RuntimeError')
                self.assertFalse(stack.cleanup_verified)
                self.assertIsNotNone(stack.lock)
            finally:
                engine.fail_remove = False
                stack.close()

    def test_bound_setup_receipt_never_serializes_error_text_or_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, _, _ = make_geometry(root); output = root / 'failed.json'
            class SecretEngine(Engine):
                def __call__(self, args, **kwargs):
                    if args[:1] == ['info']:
                        raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-ECHO')
                    return super().__call__(args, **kwargs)
            stack = qdrant.DisposableQdrant(architecture='arm64', runner=SecretEngine(), gate=lambda _: None,
                                           lock_path=root / 'slot')
            with patch.object(qdrant, 'DisposableQdrant', return_value=stack):
                result = benchmark.execute(c.path, output, engine='qdrant', duration=1, warmup=0,
                                           frozen_input_sha256=c.frozen_input_sha256)
            self.assertIsInstance(result.get('runner_failure'), dict, 'setup needs safe bound failure context')
            self.assertEqual(result['runner_failure']['primary']['phase'], 'runtime_info')
            self.assertEqual(result['bindings']['frozen_input_sha256'], c.frozen_input_sha256)
            self.assertNotIn('SYNTHETIC-SECRET-DO-NOT-ECHO', output.read_text())
            self.assertTrue(result['cleanup_verified'])

    def test_reserved_traversal_is_bounded_and_frozen_output_matches_reference(self):
        class Counted(list):
            iterations = 0
            def __iter__(self):
                self.iterations += 1
                return super().__iter__()
        for count in (24, 48):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                c, rows, vectors = make_geometry(Path(tmp), count)
                plain = c.manifest['geometry']['split']
                expected = frozen_reference(c, rows, vectors, plain)
                split = {name: Counted(plain[name]) for name in ('tuning', 'heldout')}
                actual_hash = geometry.freeze_input(c, rows, vectors, split)
                self.assertTrue(all(values.iterations <= 3 for values in split.values()),
                                'reserved-ID traversal must not scale with input row count')
                self.assertEqual((c.path / 'frozen-input.json').read_bytes(), expected)
                self.assertEqual(actual_hash, hashlib.sha256(expected).hexdigest())
