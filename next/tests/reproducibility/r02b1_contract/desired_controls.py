"""Explicit future interface BODY RED; inert observations, no host or verdicts."""
import copy
import hashlib
import importlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'src'))
inspector = importlib.import_module("cortex_core.release.inspector")


class Observations:
    def __init__(self):
        self.events = []
        self.effects = []
        self.observed = dict(image_exists=True, image_digest='sha256:'+hashlib.sha256(b'inert image').hexdigest(),
                             member_sha256=hashlib.sha256(b'inert member').hexdigest(),
                             path='/opt/cortex-inspector/inspect.py', abi='inert-source-arm64',
                             engine='podman', independent=True, custody_unchanged=True)

    def image_observation(self, ref):
        self.events.append(('observe',ref))
        return copy.deepcopy(self.observed)


class ImageInterfaceDesired(unittest.TestCase):
    def call(self, ports):
        self.assertTrue(callable(getattr(inspector,'verify_image_members',None)),
                        'R02b.1 future verified-image interface absent: expected readiness BODY RED')
        accepted = Observations().observed
        return inspector.verify_image_members('inert-source-image', accepted, ports)

    def test_positive_matching_inert_binding_never_qualifies_native(self):
        ports = Observations()
        result = self.call(ports)
        self.assertTrue(result['source_binding_verified'])
        self.assertFalse(result['native_qualified'])
        self.assertEqual(ports.effects, [])


def negative(field, value):
    def test(self):
        ports = Observations()
        ports.observed[field] = value
        result = self.call(ports)
        self.assertFalse(result['source_binding_verified'])
        self.assertFalse(result['native_qualified'])
        self.assertEqual(ports.effects, [])
    return test


for name, field, value in (
    ('missing_image','image_exists',False),
    ('changed_image','image_digest','sha256:'+hashlib.sha256(b'other image').hexdigest()),
    ('changed_member','member_sha256',hashlib.sha256(b'other member').hexdigest()),
    ('wrong_path','path','/candidate/inspect.py'),
    ('wrong_abi','abi','inert-source-amd64'),
    ('wrong_engine','engine','unknown'),
    ('candidate_authority','independent',False),
    ('custody_drift','custody_unchanged',False),
):
    setattr(ImageInterfaceDesired,'test_refuse_'+name,negative(field,value))


class ProducerInterfaceDesired(unittest.TestCase):
    def test_fixture_scope_never_dispatches_native_pair(self):
        from cortex_core.release.build import runner
        self.assertTrue(callable(getattr(runner, 'run_native_pair', None)),
                        'R02b.1 future native-producer interface absent: expected readiness BODY RED')
        ports = Observations()
        with self.assertRaises(ValueError):
            runner.run_native_pair({'qualification_scope': 'fixture'}, None, ports)
        self.assertEqual(ports.effects, [])

    def test_caller_pass_and_digest_cannot_mint_execution(self):
        from cortex_core.release.build import runner
        self.assertTrue(callable(getattr(runner, 'run_native_pair', None)),
                        'R02b.1 future native-producer interface absent: expected readiness BODY RED')
        ports = Observations()
        with self.assertRaises(ValueError):
            runner.run_native_pair({'accepted': True}, {'accepted': True, 'image_digest': ports.observed['image_digest']}, ports)
        self.assertEqual(ports.effects, [])

    def test_missing_native_input_refuses_before_dispatch(self):
        from cortex_core.release.build import runner
        self.assertTrue(callable(getattr(runner, 'run_native_pair', None)),
                        'R02b.1 future native-producer interface absent: expected readiness BODY RED')
        ports = Observations()
        with self.assertRaises(ValueError):
            runner.run_native_pair(None, None, ports)
        self.assertEqual(ports.effects, [])


if __name__=='__main__':
    unittest.main(verbosity=2)
