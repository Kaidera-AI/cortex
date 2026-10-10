"""Frozen causal controls for PR50's three findings; synthetic input only."""
import asyncio
import copy
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from vector_baseline import benchmark, corpus, geometry, qdrant


def prepared(root):
    source = root / 'synthetic.jsonl'
    source.write_text(''.join(json.dumps({'id': f'{i:064x}', 'project': 'a' * 64,
                     'type': 'document', 'month': '2026-10', 'vector': [1, i / 10]}) + '\n'
                     for i in range(24)))
    return geometry.prepare(source, root / 'corpus', dimension=2, expected_count=24)


def anchor_kwargs(call, c):
    # The same causal control executes against the missing old boundary and new API.
    if 'frozen_input_sha256' in inspect.signature(call).parameters:
        return {'frozen_input_sha256': getattr(c, 'frozen_input_sha256', '0' * 64)}
    return {}


def rehash(c):
    binding = json.loads((c.path / 'query-manifest.json').read_text())
    binding['corpus_manifest'] = corpus.digest(c.path / 'manifest.json')
    binding['queries']['heldout'] = corpus.digest(c.path / 'heldout.jsonl')
    (c.path / 'query-manifest.json').write_text(json.dumps(binding, sort_keys=True, indent=2) + '\n')


class StoredAPI:
    """Independent fake storage, including actual declared collection/index state."""
    def __init__(self, fault=None):
        self.points, self.calls, self.indexes = {}, [], {}
        self.config, self.fault = None, fault

    async def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == '/':
            return {'version': '1.19.2'}
        if method == 'PUT' and path == '/collections/b02':
            self.config = copy.deepcopy(body)
            return {'result': True}
        if '/index?' in path:
            self.indexes[body['field_name']] = {'data_type': body['field_schema']}
            return {'result': True}
        if method == 'PUT' and '/points?' in path:
            for point in body['points']:
                row = copy.deepcopy(point)
                if self.config['vectors']['distance'] == 'Cosine':
                    vector = np.asarray(row['vector'], dtype=np.float64)
                    row['vector'] = (vector / np.linalg.norm(vector)).astype('<f4').tolist()
                if row['id'] == 1:
                    if self.fault in ('project', 'generation', 'deleted'):
                        row['payload'][self.fault] = True if self.fault == 'deleted' else 'wrong'
                    elif self.fault == 'vector':
                        row['vector'] = [0, 1]
                    elif self.fault == 'comparison_id':
                        row['payload']['comparison_id'] = 'wrong'
                self.points[row['id']] = row
            return {'result': True}
        if path.endswith('/points/count'):
            return {'result': {'count': len(self.points)}}
        if method == 'POST' and path == '/collections/b02/points':
            return {'result': [copy.deepcopy(self.points[i]) for i in reversed(body['ids'])]}
        if method == 'GET' and path == '/collections/b02':
            config, indexes = copy.deepcopy(self.config), copy.deepcopy(self.indexes)
            if self.fault == 'config':
                config['vectors']['distance'] = 'Dot'
            elif self.fault == 'index':
                indexes.pop('project')
            return {'result': {'config': config, 'payload_schema': indexes}}
        raise AssertionError('unrecognized owned fake operation')


class ReviewRework(unittest.IsolatedAsyncioTestCase):
    def test_rehashed_candidate_query_source_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            c = prepared(Path(directory))
            rows = [json.loads(line) for line in (c.path / 'heldout.jsonl').read_text().splitlines()]
            query = next(row for row in rows if row['status'] == 'READY')
            query['source_id'] = c.records[0]['id'].decode()
            query['vector'] = c.vectors[0].tolist()
            (c.path / 'heldout.jsonl').write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
            rehash(c)
            caught = None
            try:
                geometry.load(c.path, **anchor_kwargs(geometry.load, c))
            except Exception as error:
                caught = type(error)
            self.assertIs(caught, ValueError, 'rehashed candidate/self query must not be heldout evidence')

    def test_rehashed_vector_and_split_order_are_refused(self):
        for fault in ('vector', 'split'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                c = prepared(Path(directory))
                if fault == 'vector':
                    rows = [json.loads(line) for line in (c.path / 'heldout.jsonl').read_text().splitlines()]
                    rows[0]['vector'] = [0, 1]
                    (c.path / 'heldout.jsonl').write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
                else:
                    data = json.loads((c.path / 'manifest.json').read_text())
                    data['geometry']['split']['heldout'], data['geometry']['split']['tuning'] = (
                        data['geometry']['split']['tuning'], data['geometry']['split']['heldout'])
                    (c.path / 'manifest.json').write_text(json.dumps(data, sort_keys=True, indent=2) + '\n')
                rehash(c)
                caught = None
                try:
                    geometry.load(c.path, **anchor_kwargs(geometry.load, c))
                except Exception as error:
                    caught = type(error)
                self.assertIs(caught, ValueError, 'current manifests cannot grant new frozen lineage')

    def test_geometry_needs_retained_anchor_and_preserves_good_input(self):
        with tempfile.TemporaryDirectory() as directory:
            c = prepared(Path(directory))
            self.assertTrue(isinstance(getattr(c, 'frozen_input_sha256', None), str), 'converter must return frozen input anchor')
            loaded = geometry.load(c.path, **anchor_kwargs(geometry.load, c))
            self.assertEqual(loaded.identity, c.identity)
            caught = None
            try:
                geometry.load(c.path)
            except Exception as error:
                caught = type(error)
            self.assertIs(caught, ValueError, 'never infer trusted anchor from current artifact')

    def test_missing_cell_writes_bound_not_run_without_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); c = prepared(root); output = root / 'missing.json'
            create = Mock(side_effect=AssertionError('engine must not start'))
            caught = None; result = None
            with patch.object(benchmark.postgres, 'DisposablePostgres', create):
                try:
                    result = benchmark.execute(c.path, output, cell='very_tight/dense', duration=1, warmup=0,
                                               **anchor_kwargs(benchmark.execute, c))
                except Exception as error:
                    caught = type(error)
            self.assertIsNone(caught, 'expected missing cell needs durable NOT_RUN, not ValueError')
            self.assertEqual(create.call_count, 0)
            self.assertTrue(output.is_file())
            stored = json.loads(output.read_text())
            self.assertEqual(stored, result)
            self.assertEqual(stored['diagnostic']['verdict'], 'NOT_RUN')
            self.assertEqual(stored['cells'][0]['status'], 'NOT_RUN')
            self.assertEqual(stored['cells'][0]['cell'], 'very_tight/dense')
            self.assertTrue(stored['cells'][0]['missing_query_ids'])
            self.assertEqual(stored['bindings']['corpus_manifest_sha256'], corpus.digest(c.path / 'manifest.json'))
            self.assertFalse(stored['engine_started'])
            self.assertFalse(stored['gtm_qualified'])

    async def test_count_only_wrong_payload_vector_config_or_index_is_refused(self):
        for fault in ('project', 'generation', 'deleted', 'vector', 'comparison_id', 'config', 'index'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                c = prepared(Path(directory)); api = StoredAPI(fault)
                caught = None
                try:
                    await qdrant.upload(api, c)
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ValueError, 'matching count cannot admit wrong engine state')

    async def test_readback_is_bounded_complete_and_handles_cosine_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            c = prepared(Path(directory)); api = StoredAPI()
            mapping, binding = await qdrant.upload(api, c)
            reads = [body for method, path, body in api.calls if method == 'POST' and path == '/collections/b02/points']
            self.assertEqual(len(reads), 1, 'import requires bounded actual point readback')
            self.assertEqual(reads[0]['ids'], [1, len(c.records) // 2 + 1, len(c.records)])
            self.assertTrue(reads[0]['with_payload'])
            self.assertTrue(reads[0]['with_vector'])
            self.assertEqual(len(mapping), len(c.records))
            self.assertTrue(binding['import_parity_verified'])
            self.assertEqual(binding['readback_sample_count'], 3)
            self.assertFalse(binding['hnsw_use_qualified'])

    def test_import_parity_refusal_writes_not_run_and_never_times_queries(self):
        self.assertTrue(hasattr(qdrant, 'ImportParityError'), 'typed parity refusal missing')
        class Stack:
            password = lock = None
            cleanup_verified = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.cleanup_verified = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); c = prepared(root); output = root / 'parity.json'
            run = Mock(side_effect=AssertionError('queries must not be timed'))
            with patch.object(qdrant, 'DisposableQdrant', return_value=Stack()), \
                    patch.object(qdrant, 'install', side_effect=qdrant.ImportParityError('synthetic mismatch')), \
                    patch.object(benchmark.load, 'run', run):
                result = benchmark.execute(c.path, output, engine='qdrant', duration=1, warmup=0,
                                           **anchor_kwargs(benchmark.execute, c))
            self.assertEqual(result['diagnostic']['verdict'], 'NOT_RUN')
            self.assertEqual(result['cells'][0]['reason'], 'qdrant-import-parity-mismatch')
            self.assertTrue(result['cleanup_verified'])
            self.assertEqual(run.call_count, 0)

    def test_altered_freeze_record_cannot_replace_retained_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            c = prepared(Path(directory))
            target = c.path / 'frozen-input.json'
            self.assertTrue(target.is_file(), 'independent frozen record missing')
            record = json.loads(target.read_text())
            record['input_count'] += 1
            target.write_text(json.dumps(record, sort_keys=True))
            caught = None
            try:
                geometry.load(c.path, **anchor_kwargs(geometry.load, c))
            except Exception as error:
                caught = type(error)
            self.assertIs(caught, ValueError)
