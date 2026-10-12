"""An in-process typed closure is not independent image verification."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_unit1_red import product


def constructed(module):
    profile = {'schema': 'cortex.release-policy.v1', 'inspector': {
        'closure_sha256': '0'*64,
        'argv': ['/opt/cortex-inspector/bin/python', '-I', '-S', '-B', '/opt/cortex-inspector/inspect.py'],
        'environment': {}, 'optimization': 0,
        'limits': dict(input_bytes=1024, output_bytes=1024, object_count=10, depth=10, seconds=5, memory_bytes=1024, pids=2, cpu_millicores=100)}}
    cap = module.InspectorClosure(module.sha256(module.canonical_bytes(profile)), '0'*64, Path('/nonexistent'), (), 'sha256:'+'0'*64, Path('/nonexistent/engine'), '0'*64, module._SEAL)
    return cap, profile


class InspectorPlanGate(unittest.TestCase):
    def test_constructed_closure_refuses_before_plan_minting(self):
        module = product('inspector')
        cap, profile = constructed(module)
        with self.assertRaisesRegex(ValueError, '^inspector_image_member_unbound$'):
            module.prepare_plan(cap, profile, b'')

    def test_inspection_refuses_constructed_closure_before_prepare(self):
        module = product('inspector')
        cap, profile = constructed(module)
        verified = SimpleNamespace(metadata={'release_policy': profile})
        with patch.object(module, 'prepare_plan', wraps=module.prepare_plan) as prepare:
            with self.assertRaisesRegex(ValueError, '^inspector_image_member_unbound$'):
                module.admit_inspection(verified, {'inspector_capability': cap})
        self.assertEqual(prepare.call_count, 0)

    def test_direct_plan_constructor_cannot_mint_unverified_plan(self):
        module = product('inspector')
        with self.assertRaisesRegex(ValueError, '^inspector_image_member_unbound$'):
            module.InspectorPlan(('nonexistent',), (), 1, 1, 1, 1, 1)
