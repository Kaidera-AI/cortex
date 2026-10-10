"""C09 in-memory literal mutants with source/raw/phase custody; no native runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

NEXT = Path(__file__).resolve().parents[2]
REPO = NEXT.parent
sys.path.insert(0, str(NEXT/'tests'))
import test_receipts as receipts  # noqa: E402

SOURCE = 'next/src/cortex_core/modules/vector/qdrant.py'
T = 'test_c09_qdrant.C09Tests.'
MUTATIONS = [
    ('descriptor-hash-bypassed', 'and hashlib.sha256(raw).hexdigest() == DESCRIPTOR_SHA256', 'and True', T+'test_descriptor_wrong_version_source_license_and_child_refuse_before_transport', 7),
    ('platform-admission-bypassed', "and type(platform) is str and platform in value['platforms']", 'and True', T+'test_unknown_platform_and_secret_bearing_descriptor_fields_refuse', 1),
    ('public-interface-admitted', 'if not any(ip in ipaddress.ip_network(network) for network in networks):', 'if False:', T+'test_private_endpoint_and_delivery_refuse_public_wildcard_host_ports_root', 3),
    ('published-port-admitted', 'or published_ports or host_network', 'or False or host_network', T+'test_private_endpoint_and_delivery_refuse_public_wildcard_host_ports_root', 1),
    ('root-user-admitted', 'or uid <= 0', 'or False', T+'test_private_endpoint_and_delivery_refuse_public_wildcard_host_ports_root', 1),
    ('equal-service-refs-admitted', 'or writer_key_ref == reader_key_ref', 'or False', T+'test_private_config_only_refs_and_rejects_missing_equal_or_path_traversal_refs', 1),
    ('version-mismatch-admitted', "if value.get('version') != self.descriptor['version']:", 'if False:', T+'test_wrong_runtime_version_missing_api_redirect_oversize_are_typed_refusals', 1),
    ('redirect-status-admitted', "or response['status'] != 200", 'or False', T+'test_wrong_runtime_version_missing_api_redirect_oversize_are_typed_refusals', 1),
    ('unbounded-response-admitted', "or len(response['body']) > MAX_RESPONSE_BYTES", 'or False', T+'test_wrong_runtime_version_missing_api_redirect_oversize_are_typed_refusals', 1),
    ('point-id-boolean-admitted', "or type(actual.get('id')) is not int", 'or False', T+'test_boolean_numeric_readback_and_coerced_delivery_flags_refuse', 1),
    ('vector-boolean-admitted', 'type(value) in (int, float) and math.isfinite(value)', 'isinstance(value, (int, float)) and math.isfinite(value)', T+'test_boolean_numeric_readback_and_coerced_delivery_flags_refuse', 1),
    ('payload-value-check-omitted', "or any(type(payload[key]) is not type(value) or payload[key] != value\n                       for key, value in expected['payload'].items())", 'or False', T+'test_boolean_numeric_readback_and_coerced_delivery_flags_refuse', 1),
    ('delivery-boolean-coerced', 'json.dumps(config, sort_keys=True) != json.dumps(expected, sort_keys=True)', 'config != expected', T+'test_boolean_numeric_readback_and_coerced_delivery_flags_refuse', 1),
    ('predicate-deletion-omitted', "('generation', 'fake-generation'), ('deleted', False)", "('generation', 'fake-generation')", T+'test_fake_requests_bind_predicates_vectors_and_separate_service_roles', 1),
    ('delete-count-check-omitted', "if self._call('POST', collection+'/points/count', {'exact': True}).get('result', {}).get('count') != 2:", 'if False:', T+'test_readback_indexes_vectors_payload_filter_delete_alias_are_verified', 1),
    ('alias-readback-check-omitted', "if self._call('GET', '/aliases').get('result', {}).get('aliases') != expected:", 'if False:', T+'test_readback_indexes_vectors_payload_filter_delete_alias_are_verified', 1),
    ('raw-protocol-error-leaked', "except Exception:\n            raise ProtocolFailure('fake_protocol_failed') from None", "except Exception as error:\n            raise ProtocolFailure(str(error)) from None", T+'test_transport_error_never_echoes_exception_text_or_retries', 1),
    ('cleanup-primary-cause-lost', "primary_reason=primary.reason if isinstance(primary, ProtocolFailure) else", "primary_reason=None if isinstance(primary, ProtocolFailure) else", T+'test_fake_partial_effect_cleanup_failure_retains_primary_and_never_retries', 1),
    ('inert-profile-false-ready', "'module_ready': False", "'module_ready': True", T+'test_inert_profile_has_no_default_transport_network_registration_or_ready', 1),
]

CHILD = r'''
import hashlib, json, pathlib, sys, unittest
from test_receipts import AssertionResult, MARKER
from cortex_core.modules.vector import qdrant
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1]) if len(sys.argv)>1 else unittest.defaultTestLoader.discover('next/tests/receipt',pattern='test_*.py')
result = unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
p=pathlib.Path(qdrant.__file__).resolve()
print(MARKER+json.dumps({'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':s} for t,s in result.errors],
 'skipped':[{'id':t.id(),'reason':s} for t,s in result.skipped],'executed_sources':{'qdrant.py':{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}}),flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    return subprocess.check_output(['git','-C',str(REPO),*args])


def snapshot():
    files={}
    for name in git('ls-tree','-r','--name-only','HEAD','--','next').decode().splitlines():
        raw=(REPO/name).read_bytes(); files[name]={'sha256':sha(raw),'bytes':len(raw)}
    return {'head':git('rev-parse','HEAD').decode().strip(),'git_tree':git('rev-parse','HEAD^{tree}').decode().strip(),
            'git_status':git('status','--porcelain').decode(),'files':files,
            'tree_sha256':sha(json.dumps(files,sort_keys=True,separators=(',',':')).encode())}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('destination',type=Path);args=parser.parse_args()
    args.destination.mkdir(parents=True,exist_ok=False)
    baseline=snapshot();assert not baseline['git_status']
    original=(REPO/SOURCE).read_bytes();rows=[]
    def execute(label,source_root,target=None,expected=None):
        before=snapshot()
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':os.pathsep.join([str(source_root),str(NEXT/'tests'),str(NEXT/'tests/receipt')])}
        result=subprocess.run([sys.executable,'-c',CHILD,*([target] if target else [])],cwd=REPO,env=env,capture_output=True,text=True)
        for key in ('stdout','stderr'):(args.destination/(label+'.'+key+'.txt')).write_text(getattr(result,key))
        after=snapshot();assert before==after==baseline
        report=receipts.report(result);assert report is not None
        imported=report['executed_sources']['qdrant.py'];assert Path(imported['path']).resolve()==source_root/'cortex_core/modules/vector/qdrant.py'
        assert imported['sha256']==sha(expected or original)
        packet={'before':before,'after':after,'target':target,'exit':result.returncode,'result':report,
                'stdout_sha256':sha(result.stdout.encode()),'stderr_sha256':sha(result.stderr.encode()),
                'expected_source_sha256':sha(expected or original),'source_root':str(source_root)}
        (args.destination/(label+'.json')).write_text(json.dumps(packet,indent=2)+'\n')
        return result,report
    clean,initial=execute('baseline',NEXT/'src');assert receipts.classify(clean,set())=='survived' and not initial['skipped']
    for label,before,after,target,bodies in MUTATIONS:
        assert original.decode().count(before)==1,label
        mutated=original.decode().replace(before,after,1).encode()
        with tempfile.TemporaryDirectory(prefix='c09-mutant-',dir=args.destination) as d:
            root=Path(d);shutil.copytree(NEXT/'src',root/'next/src');shutil.copytree(NEXT/'modules/qdrant',root/'next/modules/qdrant')
            (root/SOURCE).write_bytes(mutated)
            result,report=execute(label,root/'next/src',target,mutated)
            killed=(receipts.classify(result,{target})=='killed' and result.returncode==1 and report['tests_run']==1
                    and not report['errors'] and not report['skipped'] and len(report['failures'])==bodies)
            rows.append({'label':label,'before':before,'after':after,'target':target,'expected_body_failures':bodies,
                         'source_sha256':sha(original),'mutant_sha256':sha(mutated),'killed':killed})
            (args.destination/'in-progress.json').write_text(json.dumps(rows,indent=2)+'\n')
            print(label,'KILLED' if killed else 'INCONCLUSIVE',flush=True)
            assert killed,label
    clean,restored=execute('restored',NEXT/'src');assert receipts.classify(clean,set())=='survived' and restored['tests_run']==initial['tests_run']
    (args.destination/'mutations.json').write_text(json.dumps({'baseline':baseline,'rows':rows,'tests_run':initial['tests_run'],
        'producer_sha256':sha(Path(__file__).read_bytes()),'helper_sha256':sha(Path(receipts.__file__).read_bytes()),'child_source':CHILD},indent=2)+'\n')
    print('C09_INERT_MUTATIONS_COMPLETE',len(rows),initial['tests_run'],flush=True)


if __name__=='__main__':
    main()
