"""Freeze before repair; exercise actual old/safe controller control flow."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def exercise(kind, *, foreign=False, prepare_output=True):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        wt = root / '.worktrees/task'
        (wt / 'next').mkdir(parents=True)
        (wt / 'tmp/wheels').mkdir(parents=True)
        (wt / 'tmp/legacy-schema.sql').write_text('fixture legacy source')
        if kind == 'c03':
            out = root / 'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/c03-adoption'
            name, resource_kind, filename = 'kaidera-test-core-schema-1', 'pod', 'run-adoption.py'
        else:
            out = root / 'output/Cortex/design55-r426/2026-10-10/graph-cleanup'
            name, resource_kind, filename = 'kaidera-test-graph-cleanup-1', 'container', 'run-graph.py'
            graph = wt / 'packages/containers/graph-worker'
            graph.mkdir(parents=True)
            # Source fixture metadata only: no worker/model/application executable.
        if prepare_output:
            out.mkdir(parents=True)
        if kind == 'graph':
            out.mkdir(parents=True, exist_ok=True)
            (out / 'test_receipts.py').write_text('# unused fixture classifier metadata\n')
        labels = {'owner': 'cox@helix', 'kaidera.cox.lifecycle': 'another-invocation'}
        resources = {}  # Foreign winner appears during CREATE, after absent precheck.
        state_path = root / 'engine.json'
        state_path.write_text(json.dumps({'resources': resources, 'removed': [], 'commands': [], 'foreign_during_create': foreign, 'foreign_labels': labels}))
        binary = root / 'bin'
        binary.mkdir()
        for command in ('podman', 'git'):
            shutil.copy2(HERE / 'fake_engine.py', binary / command)
            (binary / command).chmod(0o755)
        variant = os.environ.get('REPLAY_VARIANT', 'old')
        source_path = HERE / (variant + '-' + filename)
        code = source_path.read_text()
        if kind == 'graph':
            # Relocate only the recorded root literal; keep all control flow unchanged.
            before = "ROOT = Path('/Users/amadmalik/DevVault/helix')"
            assert code.count(before) == 1
            code = code.replace(before, 'ROOT = Path(' + repr(str(root)) + ')')
            code = code.replace("WT = ROOT / '.worktrees/cox-r426-graph-cleanup-20261010'",
                                "WT = ROOT / '.worktrees/task'")
        producer = root / 'producer.py'
        producer.write_text(code)
        if (HERE / 'replay_lifecycle.py').exists():
            shutil.copy2(HERE / 'replay_lifecycle.py', root / 'replay_lifecycle.py')
        env = {**os.environ, 'PATH': str(binary) + ':' + os.environ['PATH'],
               'REPLAY_ENGINE_STATE': str(state_path), 'REPLAY_FAKE_OUT': str(out)}
        result = subprocess.run([sys.executable, str(producer), 'ack-fault'], cwd=wt,
                                env=env, capture_output=True, text=True, timeout=15)
        state = json.loads(state_path.read_text())
        state['producer_exit'] = result.returncode
        state['producer_stdout'] = result.stdout
        state['producer_stderr'] = result.stderr
        state['controller_sha256'] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        # Retain literal nested result for attribution; failed create is never a PG success.
        print('PRODUCER_FAULT_RESULT=' + json.dumps({'kind': kind, 'foreign': foreign, **state}), flush=True)
        return state


class ProducerLifecycle(unittest.TestCase):
    def test_c03_lost_ack_cleanup_reconciles_effect(self):
        state = exercise('c03')
        self.assertNotEqual(state['producer_exit'], 0)
        self.assertFalse(state['resources'])

    def test_c03_output_prepared_before_effect(self):
        state = exercise('c03', prepare_output=False)
        self.assertTrue(state['output_present_before_create'])

    def test_graph_lost_ack_cleanup_reconciles_effect(self):
        state = exercise('graph')
        self.assertNotEqual(state['producer_exit'], 0)
        self.assertFalse(state['resources'])

    def test_graph_same_owner_foreign_lifecycle_preserved(self):
        state = exercise('graph', foreign=True)
        self.assertTrue(state['resources'])
        self.assertFalse(state['removed'])

    def test_graph_removal_uses_reconciled_immutable_id(self):
        state = exercise('graph')
        self.assertEqual(len(state['removed']), 1)
        self.assertEqual(state['removed'][0]['target'], state['removed'][0]['id'])

    def test_c03_foreign_lifecycle_preserved(self):
        state = exercise('c03', foreign=True)
        self.assertTrue(state['resources'])
        self.assertFalse(state['removed'])


if __name__ == '__main__':
    unittest.main()
