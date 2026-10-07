"""Producer identity and signature-order contracts, r412."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("producer", Path(__file__).with_name("build-manual-linux.py"))
producer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(producer)

class ProducerTests(unittest.TestCase):
    def test_seven_roles_explicit_api_identity_and_no_publish(self):
        plan = producer.make_plan(Path('/source'), 'a' * 40)
        self.assertEqual(len(plan['images']), 7)
        self.assertEqual(plan['gate_order'], ['A_TEST_rehearsal', 'freeze', 'Vera_review', 'CTO_sign', 'verify_public_signature', 'B_signed_native_proof'])
        for image in plan['images']:
            argv = image['argv']
            self.assertIn('linux/amd64', argv)
            self.assertIn('KOS_SOURCE_REVISION=' + 'a' * 40, argv)
            self.assertNotIn('push', argv)
        api = next(x for x in plan['images'] if x['role'] == 'api')
        self.assertIn('CORTEX_RELEASE_ID=v0.1.003-manual.1', api['argv'])
        self.assertIn('KOS_VERSION=0.1.003-manual.1', api['argv'])

    def test_r416_api_context_contains_migration_sources(self):
        plan = producer.make_plan(Path('/source'), 'a' * 40)
        api = next(x for x in plan['images'] if x['role'] == 'api')
        self.assertEqual(api['argv'][-1], '/source/packages')
        self.assertIn('/source/packages/api/Dockerfile', api['argv'])

    def test_invalid_identity_refuses(self):
        for sha in ('main', 'a' * 39, 'A' * 40, 'a' * 40 + ';echo x'):
            with self.assertRaises(ValueError):
                producer.make_plan(Path('/source'), sha)

    def test_native_arch_is_mandatory(self):
        for host in ('Darwin', 'Windows'):
            with self.assertRaises(ValueError):
                producer.native_check(host, 'x86_64', 1000)
        for arch in ('aarch64', 'arm64'):
            with self.assertRaises(ValueError):
                producer.native_check('Linux', arch, 1000)
        with self.assertRaises(ValueError):
            producer.native_check('Linux', 'x86_64', 0)
        producer.native_check('Linux', 'x86_64', 1000)

    def test_index_cannot_be_a_platform_digest(self):
        with self.assertRaises(ValueError):
            producer.manifest_digest(b'{"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}')
        with self.assertRaises(ValueError):
            producer.manifest_digest(b'{}')

if __name__ == '__main__':
    unittest.main()
