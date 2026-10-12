"""Frozen startup contract and pre-plan image admission controls; no container."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from test_unit1_red import product

ARGV = ['/opt/cortex-inspector/bin/python', '-I', '-S', '-B', '/opt/cortex-inspector/inspect.py']


def profile():
    return {'inspector': {'argv': list(ARGV), 'environment': {}, 'optimization': 0}}


class InspectorTrust003(unittest.TestCase):
    def validator(self):
        module = product('inspector')
        self.assertTrue(callable(getattr(module, 'startup_argv', None)), 'narrow startup contract absent')
        return module.startup_argv

    def test_positive_isolated_startup_settings_are_effective(self):
        argv = self.validator()(profile())
        self.assertEqual(argv, tuple(ARGV))
        env = dict(os.environ, PYTHONHASHSEED='7', PYTHONOPTIMIZE='2', PYTHONDONTWRITEBYTECODE='0')
        result = json.loads(subprocess.check_output([sys.executable, *argv[1:-1], '-c', 'import json,sys;print(json.dumps(dict(isolated=sys.flags.isolated,ignore_environment=sys.flags.ignore_environment,no_site=sys.flags.no_site,dont_write_bytecode=sys.flags.dont_write_bytecode,optimize=sys.flags.optimize)))'], env=env, timeout=10))
        self.assertEqual(result, dict(isolated=1, ignore_environment=1, no_site=1, dont_write_bytecode=1, optimize=0))

    def test_negative_ineffective_startup_claims_refuse(self):
        validator = self.validator()
        for change in ({'optimization': 1}, {'optimization': 2}, {'optimization': False}, {'hash_seed': 7}, {'environment': {'PYTHONHASHSEED': '7'}}, {'argv': ARGV[:1] + ['-S', '-B'] + ARGV[-1:]}):
            with self.subTest(change=change):
                value = profile()
                value['inspector'].update(change)
                with self.assertRaisesRegex(ValueError, '^inspector_literal_startup$'):
                    validator(value)

    def test_unverified_image_refuses_before_decode_members_or_plan(self):
        module = product('inspector')
        raw = b'not-even-json'
        value = profile()
        value['inspector']['closure_sha256'] = module.sha256(raw)
        with patch.object(module, 'decode_json', side_effect=AssertionError('decode ran')) as decode, patch.object(module, '_read_member', side_effect=AssertionError('member read')) as read, patch.object(module, 'prepare_plan', side_effect=AssertionError('plan ran')) as plan:
            with self.assertRaisesRegex(ValueError, '^inspector_image_member_unbound$'):
                module.admit_closure(raw, module.sha256(raw), value, Path('/unread'), candidate_roots=[Path('/candidate')])
        self.assertEqual((decode.call_count, read.call_count, plan.call_count), (0, 0, 0))

    def test_manifest_digest_alone_cannot_authorize_claimed_image(self):
        module = product('inspector')
        value = profile()
        raw = module.canonical_bytes(dict(schema='cortex.inspector-closure.v1', id='neutral', image_digest='sha256:' + '0'*64, members=[]))
        digest = module.sha256(raw)
        value['inspector'].update(closure_id='neutral', closure_sha256=digest)
        with self.assertRaisesRegex(ValueError, '^inspector_image_member_unbound$'):
            module.admit_closure(raw, digest, value, Path('/unread'), candidate_roots=[Path('/candidate')])


class StartupPolicySchema(unittest.TestCase):
    def test_narrow_startup_profile_schema_accepts_only_effective_settings(self):
        from jsonschema import Draft202012Validator
        schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/release/release-policy.schema.json').read_text())
        validator = Draft202012Validator(schema['properties']['inspector'])
        value = profile()['inspector']
        value.update(scope='native', execution='contained', python_caches='CHECKED_HASH', closure_id='neutral', closure_sha256='0'*64, limits=dict(input_bytes=1, output_bytes=1, object_count=1, depth=1, seconds=1, memory_bytes=1, pids=1, cpu_millicores=1))
        validator.validate(value)
        for change in ({'hash_seed': 7}, {'optimization': 2}, {'environment': {'PYTHONHASHSEED': '7'}}):
            with self.subTest(change=change):
                bad = dict(value, **change)
                self.assertTrue(list(validator.iter_errors(bad)))
