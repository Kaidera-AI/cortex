"""Frozen geometry import/split/oracle controls; all bytes are synthetic."""
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from vector_baseline import corpus, oracle


class GeometryTests(unittest.TestCase):
    def surface(self):
        self.assertIsNotNone(importlib.util.find_spec('vector_baseline.geometry'), 'geometry importer missing')
        module = importlib.import_module('vector_baseline.geometry')
        self.assertTrue(callable(getattr(module, 'prepare', None)), 'geometry importer missing')
        return module

    def rows(self):
        return [{'id': f'{i:064x}', 'project': None if i >= 6 else 'a' * 64,
                 'type': None if i >= 6 else 'document', 'month': None if i >= 6 else '2026-10',
                 'vector': [1, i / 10]} for i in range(12)]

    def prepare(self, module, root, rows=None):
        source = root / 'geometry.jsonl'
        source.write_text(''.join(json.dumps(r) + '\n' for r in (self.rows() if rows is None else rows)))
        return module.prepare(source, root / 'out', dimension=2, expected_count=len(self.rows() if rows is None else rows))

    def test_real_admission_refuses_before_any_input_read(self):
        module = self.surface()
        with patch.object(Path, 'open', side_effect=AssertionError('input was touched')):
            with self.assertRaises(ValueError):
                module.prepare(Path('custodian-only'), Path('never-created'), dataset='marlow-geometry')
        with self.assertRaises(ValueError):
            module.validate_admission({'dataset': 'marlow-geometry'}, 'marlow-geometry')

    def test_geometry_keeps_unknown_provenance_and_stored_values(self):
        module = self.surface()
        with tempfile.TemporaryDirectory() as tmp:
            c = self.prepare(module, Path(tmp))
            self.assertEqual((c.identity['provider'], c.identity['model']), ('unknown', 'unknown'))
            self.assertEqual(c.manifest['geometry']['qualification'], 'GEOMETRY_ONLY')
            self.assertEqual(c.manifest['geometry']['unknown'], ['tenant/auth', 'deletion', 'model/provider', 'semantic-query', 'sparse'])
            for record, vector in zip(c.records, c.vectors):
                i = int(record['id'].decode(), 16)
                np.testing.assert_array_equal(vector, np.asarray([1, i / 10], dtype='<f4'))
            self.assertFalse(c.records['has_sparse'].any())

    def test_split_is_deterministic_order_independent_and_disjoint(self):
        module = self.surface()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'a').mkdir(); (root / 'b').mkdir()
            a = self.prepare(module, root / 'a')
            b = self.prepare(module, root / 'b', list(reversed(self.rows())))
            self.assertEqual(a.manifest['geometry']['split'], b.manifest['geometry']['split'])
            self.assertEqual(a.identity, b.identity)
            for name in corpus.FILES + ('tuning.jsonl', 'heldout.jsonl'):
                self.assertEqual((a.path / name).read_bytes(), (b.path / name).read_bytes(), name)
            split = a.manifest['geometry']['split']
            candidates = {r.decode() for r in a.records['id']}
            tuning, heldout = set(split['tuning']), set(split['heldout'])
            self.assertFalse(candidates & (tuning | heldout))
            self.assertFalse(tuning & heldout)
            self.assertEqual(len(candidates) + len(tuning) + len(heldout), 12)
            self.assertEqual((len(tuning), len(heldout)), (2, 2))

    def test_null_joint_coverage_and_small_groups_are_never_discarded(self):
        module = self.surface()
        rows = self.rows() + [{'id': f'{99:064x}', 'project': 'b'*64, 'type': 'rare', 'month': '2026-09', 'vector': [0, 1]}]
        with tempfile.TemporaryDirectory() as tmp:
            c = self.prepare(module, Path(tmp), rows)
            coverage = c.manifest['geometry']['coverage']
            self.assertEqual(sum(x['input'] for x in coverage), 13)
            self.assertEqual(sum(x['corpus'] for x in coverage), 9)
            null = next(x for x in coverage if x['category'] == [None, None, None])
            self.assertEqual((null['input'], null['tuning'], null['heldout']), (6, 1, 1))
            rare = next(x for x in coverage if x['category'][1] == 'rare')
            self.assertEqual((rare['corpus'], rare['status']), (1, 'NOT_RUN'))
            self.assertIn(f'{99:064x}'.encode(), c.records['id'])

    def test_full_oracle_uses_only_candidates_and_missing_strata_stay_not_run(self):
        module = self.surface()
        with tempfile.TemporaryDirectory() as tmp:
            c = self.prepare(module, Path(tmp))
            queries = [json.loads(x) for x in (c.path / 'heldout.jsonl').read_text().splitlines()]
            self.assertEqual({q['stratum'] for q in queries}, set(corpus.STRATA))
            self.assertTrue(any(q['status'] == 'NOT_RUN' for q in queries))
            for q in (x for x in queries if x['status'] == 'READY'):
                truth = oracle.rank(c, q, modes=('dense',))
                self.assertEqual(truth.eligible_count, q['eligible_count'])
                ids = truth.ids('dense')
                self.assertNotIn(q['source_id'], ids)
                self.assertFalse(set(ids) & set(c.manifest['geometry']['split']['heldout']))
                keep = oracle.eligible(c.records, q)
                v = c.vectors[keep].astype(np.float64); query = np.asarray(q['vector'], dtype=np.float64)
                scores = v @ query / (np.linalg.norm(v, axis=1) * np.linalg.norm(query))
                expected = [x.decode() for x in c.records['id'][keep][np.lexsort((c.records['id'][keep], -scores))]][:100]
                self.assertEqual(ids, expected)

    def test_malformed_geometry_refuses_without_partial_output(self):
        module = self.surface()
        changes = [{'id': 'raw-id'}, {'project': ''}, {'type': 1}, {'month': '2026-13'}, {'vector': [[1, 0]]},
                   {'vector': [True, 0]}, {'vector': [float('nan'), 0]}, {'vector': [0, 0]},
                   {'vector': [1e-40, 0]}, {'vector': [1e40, 1]}, {'vector': [1, 0, 0]}, {'text': 'forbidden'}]
        for change in changes:
            with self.subTest(change=repr(change)), tempfile.TemporaryDirectory() as tmp:
                rows = self.rows(); rows[0].update(change); root = Path(tmp)
                with self.assertRaises(ValueError):
                    self.prepare(module, root, rows)
                self.assertFalse((root / 'out').exists(), 'validation precedes output effects')
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.rows(); rows[1]['id'] = rows[0]['id']
            with self.assertRaises(ValueError):
                self.prepare(module, Path(tmp), rows)

    def test_input_count_and_query_hash_drift_are_refused(self):
        module = self.surface()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c = self.prepare(module, root)
            (c.path / 'heldout.jsonl').write_text('{}\n')
            with self.assertRaises(ValueError):
                module.load(c.path)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'input'; source.write_text(json.dumps(self.rows()[0]) + '\n')
            with self.assertRaises(ValueError):
                module.prepare(source, root / 'out', dimension=2, expected_count=12)

    def test_real_admission_is_bound_to_shape_hash_review_and_custody(self):
        module = self.surface()
        good = {'schema': 'cortex-b02-data-admission-v1', 'dataset': 'marlow-geometry', 'input_sha256': 'a'*64,
                'count': 141511, 'dimension': 768, 'cto_decision': 'H-D472-named-run',
                'custody_receipt': 'ren-tk-reviewed-custody', 'import_review_sha': 'b'*40}
        module.validate_admission(good, 'marlow-geometry')
        for key, value in [('count', 12), ('dimension', 2), ('input_sha256', ''), ('cto_decision', ''),
                           ('custody_receipt', ''), ('import_review_sha', ''), ('dataset', 'synthetic')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.validate_admission({**good, key: value}, 'marlow-geometry')
        with self.assertRaises(ValueError):
            module.admit_artifact({'dataset': 'marlow-geometry', 'geometry': {'input_sha256': 'c'*64}}, good)

    def test_native_bindings_cannot_admit_mac_cold_or_missing_gates(self):
        module = self.surface()
        with self.assertRaises(ValueError):
            module.admit_native({}, {})
        data = {'schema': 'cortex-b02-data-admission-v1', 'dataset': 'marlow-geometry', 'input_sha256': 'a'*64,
                'count': 141511, 'dimension': 768, 'cto_decision': 'named-run', 'custody_receipt': 'custody', 'import_review_sha': 'b'*40,
                'native_host_receipt': 'host', 'resize_receipt': 'resize', 'edition': 'mac', 'cache_mode': 'cold', 'cost_cap_usd': 3}
        with self.assertRaises(ValueError):
            module.admit_native({'dataset': 'marlow-geometry', 'geometry': {'input_sha256': 'a'*64, 'input_count': 141511},
                                 'identity': {'dimension': 768}}, data)

    def test_admission_rejects_unknown_secret_bearing_fields(self):
        module = self.surface()
        admission = {'schema': 'cortex-b02-data-admission-v1', 'dataset': 'marlow-geometry', 'input_sha256': 'a'*64,
                     'count': 141511, 'dimension': 768, 'cto_decision': 'named-run',
                     'custody_receipt': 'custody', 'import_review_sha': 'b'*40, 'api_key': 'SYNTHETIC-NEVER-LOG'}
        with self.assertRaises(ValueError):
            module.validate_admission(admission, 'marlow-geometry')
