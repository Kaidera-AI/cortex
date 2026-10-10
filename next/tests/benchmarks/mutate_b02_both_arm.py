"""Both-arm literal semantic mutations, all-NEXT custody and body-only verdicts."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

NEXT = Path(__file__).resolve().parents[2]
REPO = NEXT.parent
sys.path.insert(0, str(NEXT / 'tests'))
receipts = importlib.import_module('test_receipts')
G = 'test_b02_geometry.GeometryTests.'
Q = 'test_b02_qdrant.QdrantTests.'
MUTATIONS = [
    ('geometry.py', 'unknown-provenance-invented', "{'provider': 'unknown', 'model': 'unknown', 'dimension': dimension,",
     "{'provider': 'synthetic', 'model': 'unknown', 'dimension': dimension,",
     G+'test_geometry_keeps_unknown_provenance_and_stored_values', 1),
    ('geometry.py', 'unknown-admission-fields-admitted', 'or set(admission) - ADMISSION_FIELDS', 'or False',
     G+'test_admission_rejects_unknown_secret_bearing_fields', 1),
    ('geometry.py', 'private-text-field-admitted', "set(row) != {'id', 'project', 'type', 'month', 'vector'}", 'False',
     G+'test_malformed_geometry_refuses_without_partial_output', 1),
    ('geometry.py', 'subnormal-norm-admitted', 'norm < np.finfo(np.float32).tiny', 'norm <= 0',
     G+'test_malformed_geometry_refuses_without_partial_output', 1),
    ('geometry.py', 'joint-groups-not-reserved', 'ready = len(group) >= 3', 'ready = False',
     G+'test_split_is_deterministic_order_independent_and_disjoint', 1),
    ('geometry.py', 'small-group-coverage-fiction', "                         'status': 'READY' if ready else 'NOT_RUN'})", "                         'status': 'READY'})",
     G+'test_null_joint_coverage_and_small_groups_are_never_discarded', 1),
    ('geometry.py', 'marginal-count-not-aggregated', 'counts[name] += category[name]', 'counts[name] = category[name]',
     G+'test_joint_and_marginal_split_coverage_reconcile_including_nulls', 1),
    ('geometry.py', 'query-hash-drift-admitted', 'if binding[\'queries\'][name] != corpus.digest(target):', 'if False:',
     G+'test_input_count_and_query_hash_drift_are_refused', 1),
    ('geometry.py', 'mac-cold-admitted', "or (admission['edition'] == 'mac' and admission['cache_mode'] != 'warm')", 'or False',
     G+'test_native_bindings_cannot_admit_mac_cold_or_missing_gates', 1),
    ('qdrant.py', 'root-containers-admitted', "'--user', '10001:10001', '--init'", "'--user', '0:0', '--init'",
     Q+'test_pins_nonroot_private_network_and_resource_envelope', 1),
    ('qdrant.py', 'published-port-added', "'--pids-limit=128', '--network', self.network_name,",
     "'--pids-limit=128', '-p', '127.0.0.1::6333', '--network', self.network_name,",
     Q+'test_pins_nonroot_private_network_and_resource_envelope', 1),
    ('qdrant.py', 'internal-network-disabled', "['network', 'create', '--internal', *self.labels(), self.network_name]",
     "['network', 'create', *self.labels(), self.network_name]", Q+'test_pins_nonroot_private_network_and_resource_envelope', 1),
    ('qdrant.py', 'qdrant-memory-doubled', "'--memory=768m', '--cpus=1.5'", "'--memory=1536m', '--cpus=1.5'",
     Q+'test_pins_nonroot_private_network_and_resource_envelope', 1),
    ('qdrant.py', 'tmpfs-nonroot-ownership-disabled', 'tmpfs-mode=0700,U=true', 'tmpfs-mode=0700,U=false',
     Q+'test_storage_mount_uses_supported_nonroot_tmpfs_ownership', 1),
    ('qdrant.py', 'below-memory-floor-admitted', 'free_percent < 35', 'free_percent < 34',
     Q+'test_memory_and_team_slot_gate_refuse_before_effect', 1),
    ('qdrant.py', 'pending-create-after-ack',
     '        self.pending.add((kind, name))  # Effect may happen before its acknowledgement.\n        self.runner(args, **kwargs)',
     '        self.runner(args, **kwargs)\n        self.pending.add((kind, name))',
     Q+'test_pending_creates_are_cleaned_and_foreign_collisions_preserved', 3),
    ('qdrant.py', 'failed-cleanup-releases-custody', '        if self.cleanup_verified:',
     '        if self.cleanup_verified or errors:', Q+'test_failed_cleanup_retains_lock_and_credential_until_verified_absence', 1),
    ('qdrant.py', 'deleted-payload-visible', "conditions.append({'key': 'deleted', 'match': {'value': False}})",
     "conditions.append({'key': 'deleted', 'match': {'value': True}})", Q+'test_dense_request_preserves_all_filters_and_refuses_sparse', 1),
    ('qdrant.py', 'filter-upper-bound-drift', "[('gte', lo), ('lte', hi)]", "[('gte', lo), ('lt', hi)]",
     Q+'test_dense_request_preserves_all_filters_and_refuses_sparse', 1),
    ('qdrant.py', 'wrong-point-payload-admitted', "or point['payload']['comparison_id'] != sid", 'or False',
     Q+'test_response_mapping_refuses_unknown_duplicate_or_payload_mismatch', 1),
    ('qdrant.py', 'owned-proxy-stop-skipped', '                    await self.stack.stop_proxy(self.pid)', '                    pass',
     Q+'test_persistent_proxy_child_is_reaped_after_cancel_and_partial_open', 1),
    ('qdrant.py', 'cancelled-client-not-closed', '        self.closed = True', '        self.closed = False',
     Q+'test_proxy_cancellation_reaps_pending_response_child', 1),
    ('qdrant.py', 'cosine-import-as-dot', "{'cosine': 'Cosine', 'dot': 'Dot', 'euclidean': 'Euclid'}",
     "{'cosine': 'Dot', 'dot': 'Dot', 'euclidean': 'Euclid'}", Q+'test_upload_binds_stored_geometry_payload_indexes_and_exact_count', 1),
    ('qdrant.py', 'import-count-mismatch-admitted', "if count['result']['count'] != len(c.records):", 'if False:',
     Q+'test_upload_binds_stored_geometry_payload_indexes_and_exact_count', 1),
    ('qdrant.py', 'observed-root-user-admitted', "config.get('User') != '10001:10001'", 'False',
     Q+'test_observed_container_contract_rejects_root_ports_and_env_keys', 1),
    ('qdrant.py', 'observed-published-port-admitted', "or any(host.get('PortBindings', {}).values())", 'or False',
     Q+'test_observed_container_contract_rejects_root_ports_and_env_keys', 1),
    ('qdrant.py', 'effective-capability-admitted', "row['EffectiveCaps'] in (None, [])", 'True',
     Q+'test_podman_empty_capability_sets_are_admitted_but_nonempty_refused', 1),
    ('qdrant.py', 'bounding-capability-admitted', "row['BoundingCaps'] in (None, [])", 'True',
     Q+'test_podman_empty_capability_sets_are_admitted_but_nonempty_refused', 1),
    ('qdrant.py', 'partial-import-reopened', '            return await upload(client, c)',
     '            try:\n                return await upload(client, c)\n            except RuntimeError:\n                await client.open()\n                return await upload(client, c)',
     Q+'test_partial_import_failure_is_not_retried', 1),
    ('qdrant_proxy.py', 'dependency-error-text-leaked', "'error': type(error).__name__", "'error': str(error)",
     Q+'test_proxy_sanitizes_failure_text_and_keeps_sequence', 1),
    ('benchmark.py', 'client-resource-observation-omitted', '    client = await observe_owned(adapter)', '    client = q',
     Q+'test_dual_observation_includes_both_owned_containers', 1),
    ('benchmark.py', 'invalid-budget-read-input', 'if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms <= 0:',
     'if False:', Q+'test_invalid_latency_budget_refuses_before_input_read', 1),
    ('postgres.py', 'nemo-pg-precreate-gate-skipped', '                self.preflights.append(preflight(self.runner))',
     '                pass', Q+'test_pg_arm_preserves_default_and_applies_nemo_slot_gate', 1),
]

CHILD = r'''
import hashlib, importlib, json, pathlib, sys, unittest
from test_receipts import AssertionResult, MARKER
tests = pathlib.Path(sys.argv[1])
target = sys.argv[2:]
suite = (unittest.defaultTestLoader.loadTestsFromName(target[0]) if target else
         unittest.defaultTestLoader.discover(str(tests), pattern='test_*.py'))
result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(suite)
sources = {}
for name in ['load', 'report', 'benchmark', 'corpus', 'oracle', 'postgres', 'geometry', 'qdrant', 'qdrant_proxy']:
    module = importlib.import_module('vector_baseline.' + name)
    path = pathlib.Path(module.__file__).resolve()
    sources[name + '.py'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
print(MARKER + json.dumps({'tests_run': result.testsRun, 'failures': result.assertions,
                          'errors': [{'id': t.id(), 'traceback': s} for t, s in result.errors],
                          'skipped': [{'id': t.id(), 'reason': s} for t, s in result.skipped],
                          'executed_sources': sources}), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args])


def snapshot():
    paths = set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', 'next').decode().split('\0')) - {''}
    files = {name: {'sha256': sha((REPO/name).read_bytes()), 'bytes': (REPO/name).stat().st_size} for name in sorted(paths)}
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'git_tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
            'git_status': git('status', '--porcelain').decode(), 'files': files,
            'tree_sha256': sha(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    baseline = snapshot()
    if baseline['git_status']:
        raise RuntimeError('INCONCLUSIVE: commit and clean all source before proof')
    originals = {p.name: p.read_bytes() for p in (NEXT/'src/vector_baseline').glob('*.py')}
    labels = set()
    for filename, label, before, after, target, bodies in MUTATIONS:
        if label in labels or originals[filename].decode().count(before) != 1 or before == after or bodies < 1:
            raise RuntimeError('INCONCLUSIVE: literal recipe drift: '+label)
        compile(originals[filename].decode().replace(before, after), filename, 'exec')
        labels.add(label)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('B01_PODMAN', None)
    rows = []

    def execute(label, root, expected, target=None):
        before = snapshot()
        path = os.pathsep.join([str(root), str(NEXT/'tests/benchmarks'), str(NEXT/'tests'), str(NEXT/'src')])
        child = subprocess.run([sys.executable, '-c', CHILD, str(NEXT/'tests/benchmarks'), *([target] if target else [])],
                               env={**env, 'PYTHONPATH': path}, capture_output=True, text=True, timeout=90)
        for stream in ('stdout', 'stderr'):
            (args.destination/(label+'.'+stream+'.txt')).write_text(getattr(child, stream))
        after = snapshot()
        parsed = receipts.report(child)
        imported = parsed.get('executed_sources', {}) if parsed else {}
        custody = (set(imported) == set(expected) and all(row['sha256'] == sha(expected[name])
                    and Path(row['path']).resolve().is_relative_to(root.resolve()) for name, row in imported.items()))
        files = dict(baseline['files'])
        for name, data in expected.items():
            files['next/src/vector_baseline/'+name] = {'sha256': sha(data), 'bytes': len(data)}
        packet = {'before': before, 'after': after, 'source_stable': before == after == baseline,
                  'executed_files': files, 'executed_tree_sha256': sha(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()),
                  'executed_sources': imported, 'executed_root': str(root.resolve()), 'import_custody': custody,
                  'target': target, 'exit': child.returncode, 'result': parsed,
                  'stdout_sha256': sha(child.stdout.encode()), 'stderr_sha256': sha(child.stderr.encode())}
        (args.destination/(label+'.json')).write_text(json.dumps(packet, indent=2)+'\n')
        if not packet['source_stable'] or not custody:
            raise RuntimeError('INCONCLUSIVE: source/import custody failed: '+label)
        return child, parsed, packet

    def clean(label):
        child, parsed, packet = execute(label, NEXT/'src', originals)
        if child.returncode or not parsed or parsed['failures'] or parsed['errors']:
            raise RuntimeError('INCONCLUSIVE: full suite not clean: '+label)
        return parsed, packet

    initial, _ = clean('baseline')
    for filename, label, before, after, target, bodies in MUTATIONS:
        expected = dict(originals)
        expected[filename] = originals[filename].decode().replace(before, after).encode()
        with tempfile.TemporaryDirectory(prefix='b02-both-arm-mutant-', dir=args.destination) as directory:
            root = Path(directory); package = root/'vector_baseline'; package.mkdir()
            for name, data in expected.items():
                (package/name).write_bytes(data)
            child, parsed, packet = execute(label, root, expected, target)
            failures = parsed['failures']
            killed = (receipts.classify(child, {target}) == 'killed' and parsed['tests_run'] == 1
                      and len(failures) == bodies and not parsed['errors'] and not parsed.get('skipped')
                      and {f['id'].split(' (')[0] for f in failures} == {target})
            row = {'label': label, 'file': filename, 'target': target, 'expected_body_failures': bodies,
                   'recipe': {'before': before, 'after': after}, 'killed': killed,
                   'classification': 'KILLED' if killed else 'INCONCLUSIVE',
                   'exit': child.returncode, 'body_failures': len(failures), 'errors': len(parsed['errors']),
                   'head': baseline['head'], 'baseline_tree_sha256': baseline['tree_sha256'],
                   'executed_tree_sha256': packet['executed_tree_sha256'], 'source_sha256': sha(originals[filename]),
                   'mutant_sha256': sha(expected[filename]), 'packet_sha256': sha((args.destination/(label+'.json')).read_bytes())}
            rows.append(row)
            (args.destination/'in-progress.json').write_text(json.dumps(rows, indent=2)+'\n')
            print(label, row['classification'], flush=True)
            if not killed:
                raise RuntimeError('INCONCLUSIVE: expected body-only kill missing: '+label)
    restored, _ = clean('restored')
    if initial['tests_run'] != restored['tests_run'] or initial.get('skipped') != restored.get('skipped'):
        raise RuntimeError('INCONCLUSIVE: baseline/restored inventory differs')
    (args.destination/'mutations.json').write_text(json.dumps({'mutations': rows, 'baseline': baseline,
            'tests_run': initial['tests_run'], 'skipped': initial.get('skipped'),
            'recipe_file_sha256': sha(Path(__file__).read_bytes()),
            'helper_sha256': sha(Path(receipts.__file__).read_bytes())}, indent=2)+'\n')
    print(json.dumps({'mutants': len(rows), 'actual_body_kills': len(rows), 'baseline_restored': initial['tests_run'],
                      'skipped': initial.get('skipped')}))


if __name__ == '__main__':
    main()
