"""U3-A frozen wire tests: structural schema, never release authorization.

Declared fixture members/root length/owner receipts are deliberately fictional.
No member bytes, keys, signatures, transport or verifier are used here.
Semantic/original-byte guards are frozen later-body obligations, not JSON Schema claims.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

from jsonschema import Draft202012Validator

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / 'src'))
from cortex_core.release.codec import canonical_bytes, decode_json

FIXTURE = Path(__file__).resolve().parent / 'r01u3_support'
MANIFEST_RAW = (FIXTURE / 'aggregate.canonical.json').read_bytes()
MANIFEST = json.loads(MANIFEST_RAW)
POLICY = json.loads((FIXTURE / 'production-policy.shape.json').read_bytes())
SUBJECTS = json.loads((FIXTURE / 'SUBJECTS.json').read_bytes())


def validate(self, value, name):
    path = NEXT / 'contracts/release' / (name+'.schema.json')
    self.assertTrue(path.is_file(), 'SURFACE_ABSENCE: frozen '+name+' schema')
    schema = json.loads(path.read_bytes())
    Draft202012Validator.check_schema(schema)
    return list(Draft202012Validator(schema).iter_errors(value))


class Contract(unittest.TestCase):
    def test_manifest_schema_exists(self):
        self.assertFalse(validate(self, MANIFEST, 'aggregate'))

    def test_policy_schema_exists(self):
        self.assertFalse(validate(self, POLICY, 'aggregate-policy'))

    def test_expected_original_bytes_hash_and_domain_comment(self):
        self.assertEqual(canonical_bytes(MANIFEST), MANIFEST_RAW)
        digest = hashlib.sha256(MANIFEST_RAW).hexdigest()
        self.assertEqual(digest, SUBJECTS['manifest_sha256'])
        self.assertEqual('sha256:'+digest, SUBJECTS['aggregate_id'])
        self.assertEqual(SUBJECTS['expected_trusted_comment'],
            'cortex.release-aggregate.v1|purpose=cortex/core-api/test|release=v0.1.020-fixture|sequence=1|sha256='+digest+'|source='+'a'*40)
        self.assertLessEqual(len(SUBJECTS['expected_trusted_comment'].encode()), 512)
        self.assertEqual(POLICY['expected_aggregate_sha256'], digest)
        self.assertEqual(POLICY['expected_members'], MANIFEST['members'])
        self.assertEqual(SUBJECTS['classification'], 'INERT_SCHEMA_SHAPE_ONLY_UNBOUND')
        self.assertFalse(SUBJECTS['actual_native_qualified'])
        self.assertEqual(SUBJECTS['signature'], 'NOT_PRESENT_NOT_GENERATED')

    def test_existing_codec_refuses_duplicate_keys_and_reencoding(self):
        with self.assertRaises(ValueError):
            decode_json(b'{"schema":1,"schema":2}\n',1048576)
        with self.assertRaises(ValueError):
            decode_json(b'{ "schema": 1 }\n',1048576)

    def test_semantic_path_uniqueness_is_not_claimed_as_schema_proof(self):
        value = copy.deepcopy(MANIFEST)
        extra = copy.deepcopy(next(row for row in value['members'] if row['kind']=='record'));extra['sha256']='f'*64
        value['members'].append(extra)
        # JSON Schema uniqueItems detects whole duplicates only. R01 must independently
        # reject duplicate logical paths/signed closure; this shape PASS is UNBOUND.
        self.assertFalse(validate(self, value, 'aggregate'))
        self.assertIn('unsorted_or_duplicate_paths', SUBJECTS['semantic_future_refusals'])


def invalid_case(schema, label, mutate):
    def test(self):
        value = copy.deepcopy(MANIFEST if schema=='aggregate' else POLICY)
        mutate(value)
        self.assertTrue(validate(self, value, schema), 'BODY: schema accepted '+label)
    return test


MANIFEST_CASES = {
    'unknown_top': lambda v:v.update(extra=True),
    'unknown_release': lambda v:v['release'].update(extra=True),
    'unknown_source': lambda v:v['source'].update(extra=True),
    'unknown_compatibility': lambda v:v['compatibility'].update(extra=True),
    'unknown_member': lambda v:v['members'][0].update(extra=True),
    'schema_kos': lambda v:v.update(schema='kaidera-os.release.v1'),
    'format_future': lambda v:v.update(format_version=2),
    'format_boolean': lambda v:v.update(format_version=True),
    'purpose_kos': lambda v:v.update(purpose='kos/build-a/test'),
    'purpose_alias': lambda v:v.update(purpose='Cortex/core-api/test'),
    'sequence_zero': lambda v:v.update(sequence=0),
    'sequence_boolean': lambda v:v.update(sequence=True),
    'sequence_overflow': lambda v:v.update(sequence=9223372036854775808),
    'sequence_fractional': lambda v:v.update(sequence=1.5),
    'negative_time': lambda v:v.update(not_before=-1),
    'source_prefix': lambda v:v['source'].update(commit='a'*39),
    'source_uppercase': lambda v:v['source'].update(commit='A'*40),
    'bad_release_id': lambda v:v['release'].update(release_id='v1|purpose=kos'),
    'bool_schema_version': lambda v:v['release'].update(schema_version=True),
    'missing_lock': lambda v:v.update(members=[r for r in v['members'] if r['kind']!='runtime-lock']),
    'missing_image_pair': lambda v:v.update(members=[r for r in v['members'] if r.get('target')!='linux/amd64']),
    'duplicate_exact_member': lambda v:v['members'].append(copy.deepcopy(v['members'][0])),
}
for label,bad in [('absolute','/tmp/x'),('dotdot','members/../x'),('double_slash','members//x'),('dot','members/./x'),('backslash','members\\x'),('uppercase','members/X'),('unicode','members/é'),('percent','members/%2e%2e/x'),('drive','C:/x')]:
 MANIFEST_CASES['path_'+label]=lambda v,bad=bad:v['members'][0].update(path=bad)
for field,bad in [('bytes',0),('bytes',True),('bytes',4294967297),('sha256','f'*63),('sha256','F'*64),('target','linux/s390x'),('media_type','text/plain'),('role','other-image')]:
 label='image_'+field+'_'+str(bad).replace('/','_')
 def mutation(v,field=field,bad=bad):
  next(r for r in v['members'] if r['kind']=='image')[field]=bad
 MANIFEST_CASES[label]=mutation
for field in ['schema','purpose','release','source','sequence','not_before','expires_at','compatibility','members']:
 MANIFEST_CASES['missing_'+field]=lambda v,field=field:v.pop(field)

POLICY_CASES={
 'unknown_top':lambda v:v.update(trusted=True),
 'artifact_root':lambda v:v['root'].update(from_artifact=True),
 'test_root_mode':lambda v:v['root'].update(test_root=True),
 'legacy_algorithm':lambda v:v['root'].update(signature_algorithm='Ed'),
 'wrong_root_id':lambda v:v['root'].update(key_id='0000000000000000'),
 'missing_root_hash':lambda v:v['root'].pop('key_material_sha256'),
 'candidate_public_path':lambda v:v['root'].update(public_key_ref='../candidate.pub'),
 'unknown_owner':lambda v:v['owner_binding'].update(trusted=True),
 'negative_fence':lambda v:v['installed_baseline'].update(fence=-1),
 'malformed_installation':lambda v:v['owner_binding'].update(installation_id='not-a-uuid'),
 'bad_current_digest':lambda v:v.update(expected_aggregate_sha256='f'*63),
 'bool_generation':lambda v:v['owner_binding'].update(policy_generation=True),
 'empty_root_purposes':lambda v:v['root'].update(allowed_purposes=[]),
 'kos_root_permission':lambda v:v['root'].update(key_use_permission='kos-build'),
}
for field,maximum in [('manifest_bytes',1048576),('signature_bytes',4096),('lock_bytes',262144),('record_bytes',8388608),('image_bytes',4294967296),('aggregate_bytes',17179869184),('member_count',10000),('depth',32),('read_seconds',120)]:
 POLICY_CASES['overflow_'+field]=lambda v,field=field,maximum=maximum:v['limits'].update({field:maximum+1})
 POLICY_CASES['bool_'+field]=lambda v,field=field:v['limits'].update({field:True})
for field in ['root','purpose','release','source','sequence_floor','expected_aggregate_sha256','expected_members','installed_baseline','validity','limits','owner_binding']:
 POLICY_CASES['missing_'+field]=lambda v,field=field:v.pop(field)

for schema,cases in [('aggregate',MANIFEST_CASES),('aggregate-policy',POLICY_CASES)]:
 for label,mutate in cases.items():
  setattr(Contract,'test_reject_'+schema.replace('-','_')+'_'+label,invalid_case(schema,label,mutate))


if __name__=='__main__':unittest.main(verbosity=2)
