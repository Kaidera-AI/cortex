"""Canonical v3 descriptor shape; receiver-only obligations are not admission."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator

NEXT = Path(__file__).resolve().parents[2]
SCHEMA = NEXT / 'contracts/deployment/cortex-prerequisite-v3.schema.json'
VECTORS = Path(__file__).with_name('cortex-prerequisite-v3-vectors.json')
SCHEMA_SHA = '513db458da1bca2c4ff7dadbe7ef5a3c26a8fe2f00fbb15c0a132d2742869d0b'
VECTORS_SHA = '1978609284e7db5138420fc77d3509ac4b61795ff8e7a4aad35a6123d62e5ebd'
REQUIRED = ['schema', 'api_url', 'installation_id', 'owner_uid', 'release_id', 'release_lineage', 'release_sequence', 'api_contract', 'source_revision', 'platform', 'host_os', 'release_manifest', 'release_signature', 'images', 'health_path', 'credential_dir', 'credential_file', 'credential_project', 'credential_agent', 'runtime', 'install_guide']
ROLES = ['db', 'migrate', 'api', 'graph', 'embed', 'pdf', 'audio', 'vision']
CASE_COUNT = 108
RECEIVER_ONLY_COUNT = 17

class PrerequisiteV3Contract(unittest.TestCase):
    def schema(self):
        self.assertTrue(SCHEMA.is_file(), 'canonical v3 descriptor schema missing')
        value = json.loads(SCHEMA.read_bytes())
        Draft202012Validator.check_schema(value)
        return value

    def vectors(self):
        return json.loads(VECTORS.read_bytes())

    def test_schema_presence_exact_identity_and_frozen_digest(self):
        value = self.schema()
        self.assertEqual(value['$id'], 'urn:kaidera:cortex:prerequisite:v3')
        self.assertEqual(value['properties']['schema']['const'], 'cortex.prerequisite.v3')
        self.assertEqual(hashlib.sha256(SCHEMA.read_bytes()).hexdigest(), SCHEMA_SHA)
        self.assertEqual(hashlib.sha256(VECTORS.read_bytes()).hexdigest(), VECTORS_SHA)
        self.assertEqual(self.vectors()['schema_sha256'], SCHEMA_SHA)

    def test_closed_field_sets_and_local_refs(self):
        value = self.schema()
        self.assertEqual(value['required'], REQUIRED)
        self.assertEqual(set(value['properties']), set(REQUIRED))
        self.assertEqual(value['$defs']['runtime']['required'], ['profile_id', 'policy_sha256', 'binding'])
        self.assertEqual(value['$defs']['binding']['required'], ['machine_name', 'connection_name'])
        self.assertEqual(set(value['$defs']['images']['properties']), set(ROLES))
        objects = []
        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'object': objects.append(node)
                if '$ref' in node: self.assertTrue(node['$ref'].startswith('#/'))
                for child in node.values(): walk(child)
            elif isinstance(node, list):
                for child in node: walk(child)
        walk(value)
        self.assertEqual(len(objects), 4)
        self.assertTrue(all(obj.get('additionalProperties') is False for obj in objects))
        self.assertNotIn('podman', value['properties'])

    def test_shape_vectors(self):
        validator = Draft202012Validator(self.schema())
        document = self.vectors()
        self.assertEqual(len(document['cases']), CASE_COUNT)
        self.assertEqual(len({x['id'] for x in document['cases']}), CASE_COUNT)
        seen = []
        for case in document['cases']:
            with self.subTest(case=case['id']):
                raw = base64.b64decode(case['raw_base64'], validate=True)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case['raw_sha256'])
                if case['expected_schema_valid'] is not None:
                    self.assertIs(validator.is_valid(json.loads(raw)), case['expected_schema_valid'])
                else:
                    self.assertEqual(case['required_phase'], 'raw')
                    self.assertEqual(case['required_receiver_verdict'], 'REFUSE')
                seen.append(case['id'])
        self.assertEqual(len(seen), CASE_COUNT)

    def test_receiver_obligations_do_not_become_shape_admission(self):
        self.schema()
        document = self.vectors()
        held = [case for case in document['cases'] if case['required_phase'] != 'shape']
        self.assertEqual(len(held), RECEIVER_ONLY_COUNT)
        self.assertTrue(all(case['required_receiver_verdict'] == 'REFUSE' for case in held))
        self.assertEqual(document['descriptor_byte_limit'], 65536)
        positives = [case for case in document['cases'] if case['required_phase'] == 'shape' and case['expected_schema_valid']]
        self.assertEqual(len(positives), 5)
        self.assertTrue(all(case['required_receiver_verdict'] == 'FURTHER_AUTHORITY_REQUIRED' for case in positives))
        self.assertTrue(any(case['id'] == 'RF3-R-FLOAT-OWNER' and case['expected_schema_valid'] for case in held))
        self.assertTrue(any(case['id'] == 'RF3-S-CREDENTIAL-RELATION' and case['expected_schema_valid'] for case in held))
        self.assertTrue(any(case['id'] == 'RF3-C-OLD-READER-NEW-CARRIER' for case in held))

    def test_trailing_line_feed_is_refused_by_string_shapes(self):
        validator = Draft202012Validator(self.schema())
        cases = {case['id']: json.loads(base64.b64decode(case['raw_base64']))
                 for case in self.vectors()['cases']
                 if case['id'] in ('RF3-P-LINUX-AMD64', 'RF3-P-MACOS-ARM64')}
        linux, mac = cases['RF3-P-LINUX-AMD64'], cases['RF3-P-MACOS-ARM64']
        for field in ('api_url', 'installation_id', 'release_id', 'release_lineage',
                      'api_contract', 'source_revision', 'release_manifest',
                      'release_signature', 'credential_dir', 'credential_file',
                      'credential_project', 'install_guide'):
            with self.subTest(field=field):
                value = copy.deepcopy(linux)
                value[field] += '\n'
                self.assertFalse(validator.is_valid(value))
        for field in ('machine_name', 'connection_name'):
            with self.subTest(binding=field):
                value = copy.deepcopy(mac)
                value['runtime']['binding'][field] += '\n'
                self.assertFalse(validator.is_valid(value))
        for role in ('db', 'migrate', 'api'):
            with self.subTest(image=role):
                value = copy.deepcopy(linux)
                value['images'][role] += '\n'
                self.assertFalse(validator.is_valid(value))

if __name__ == '__main__':
    unittest.main()
