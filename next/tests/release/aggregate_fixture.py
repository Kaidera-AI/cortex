"""NEW synthetic, inert fixture originals. No keys, signatures or native claim.

Original R01 paths/media/receipt bytes are never rewritten. The explicit mapping
is scoped to each pair; distinct originals always occupy distinct transport paths.
Tiny OCI originals use binary evidence envelopes; this is not a native mapping
ruling. RC0 historical SQL constants are inherited fixture declarations only.
"""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from cortex_core.release.codec import canonical_bytes as encode, sha256
from cortex_core.release.contracts import freeze
from cortex_core.release.trust import _OwnerContext
import native_matrix_fixture as producer

NEXT = Path(__file__).resolve().parents[2]
SUPPORT = NEXT / 'tests/release/r01u3_support'
RC0 = NEXT / 'docs/runtime/rc0/fixtures/paired-complete.json'
VERSION = '0.1.020'
COMPAT = {'version':1, 'api_contract':'fixture-api-v1', 'modules':[
    {'module_id':m,'module_version':VERSION,'interface_version':1,
     'schema_version':1,'event_version':1} for m in ('core','api')]}


def materials():
    value = json.loads((SUPPORT / 'aggregate.canonical.json').read_bytes())
    value['release'].update(core_module_version=VERSION, api_module_version=VERSION)
    value['source'] = {'commit':'a'*40,'tree':'b'*40}
    rows = []; bodies = {}; builds = []; descriptors = []
    def add(path, raw, role='evidence', kind='record', **extra):
        assert path not in bodies
        bodies[path] = raw
        media = 'application/json' if raw[:1] in (b'{',b'[') else 'application/octet-stream'
        row = dict(kind=kind,role=role,path=path,media_type=media,bytes=len(raw),sha256=sha256(raw),**extra)
        rows.append(row); return row
    original_seed = producer.build_input
    def seed():
        document, inputs = original_seed()
        inputs[document['compatibility_inputs']['compatibility_contract']['path']] = encode(COMPAT)
        return document, inputs
    for module in ('core','api'):
        for arch in ('arm64','amd64'):
            name = module+'-'+arch
            with patch.object(producer, 'build_input', seed):
                document, inputs, receipt, outputs, evidence, policy = producer.family(module,arch)
            paths = {}
            image_path = 'members/'+module+'-linux-'+arch+'.oci.tar'
            for group, originals in (('inputs',inputs),('outputs',outputs),('evidence',evidence)):
                for original, raw in sorted(originals.items()):
                    path = 'members/'+name+'/'+group+'/'+original.lower()
                    if original == 'A/archive.bin' and group == 'outputs':
                        path = image_path
                        row = add(path,raw,module+'-image','image',module_id=module,module_version=VERSION,
                                  target='linux/'+arch,oci_manifest_sha256=receipt['builds']['A']['oci']['manifest'],
                                  build_input_sha256=sha256(encode(document)),build_receipt_sha256=sha256(encode(receipt)))
                        row['media_type'] = 'application/x-tar'
                    else:
                        role = ('sbom' if original.endswith('/sbom.json') else
                                'provenance' if original.endswith('/provenance.json') else 'evidence')
                        if group == 'inputs' and original == document['descriptor']['path']:role='descriptor'
                        add(path,raw,role)
                    assert original not in paths, 'producer original input/output/evidence namespaces intersect'
                    paths[original] = path
            input_path = 'members/'+name+'/build-input.json'
            receipt_path = 'members/'+name+'/build-receipt.json'
            add(input_path,encode(document),'build-input'); add(receipt_path,encode(receipt),'build-receipt')
            descriptors.append(paths[document['descriptor']['path']])
            builds.append(dict(image=image_path,input=input_path,receipt=receipt_path,paths=paths,policy=policy))
    compat_paths = {}
    for role, raw in (
        ('compatibility',encode(COMPAT)),('schema',encode({'version':1,'migrations':[]})),
        ('events',encode({'version':1,'producers':['core','api']})),('migrations',encode({'version':1,'migrations':[]})),
        ('lock-schema',(NEXT/'contracts/runtime/runtime-lock-v1.schema.json').read_bytes()),
        ('config-schema',(NEXT/'contracts/runtime/runtime-config-v1.schema.json').read_bytes()),
        ('operations',(NEXT/'contracts/runtime/runtime-operations-v1.json').read_bytes())):
        path = 'members/contracts/'+role+'.json'; add(path,raw,role); compat_paths[role]=path
    value['compatibility'].update(descriptor_members=sorted(descriptors),
        compatibility_member=compat_paths['compatibility'],schema_member=compat_paths['schema'],
        event_member=compat_paths['events'],migration_member=compat_paths['migrations'],operations_member=compat_paths['operations'])
    lock = json.loads(RC0.read_bytes())['lock']
    lock['source']['value'].update(revision=value['source']['commit'],tree=value['source']['tree'])
    lock['release']['value'] = copy.deepcopy(value['release'])
    for row in rows:
        if row['kind']=='image':
            bound = lock['targets'][row['target']][row['module_id']]['image']['value']
            bound.update(sha256=row['oci_manifest_sha256'],version=VERSION,abi=row['target'])
    for key,role in (('lock_schema','lock-schema'),('config_schema','config-schema'),('operations','operations')):
        raw=bodies[compat_paths[role]]
        lock['contract_members'][key]['value'].update(sha256=sha256(raw),bytes=len(raw))
    add('runtime.lock.json',encode(lock),'runtime-lock','runtime-lock')['media_type']='application/json'
    value['members'] = sorted(rows,key=lambda r:r['path'])
    policy = json.loads((SUPPORT/'production-policy.shape.json').read_bytes())
    policy.update(purpose=value['purpose'],release=value['release'],source=value['source'],
                  expected_aggregate_sha256=sha256(encode(value)),expected_members=copy.deepcopy(value['members']))
    policy['limits'].update(image_bytes=768*1024**2,record_bytes=8*1024**2,aggregate_bytes=16*1024**3)
    policy['root']['allowed_purposes']=[value['purpose']]
    return value,bodies,builds,policy


def context(policy):
    raw=encode(policy)
    state={'installed_baseline':copy.deepcopy(policy['installed_baseline']),
           'policy_generation':policy['owner_binding']['policy_generation'],'fence':1}
    return _OwnerContext(freeze(policy),raw,sha256(raw),b'UNUSED-INERT-FIXTURE-ROOT\n',1500,
                         state['policy_generation'],state['fence'],'owner',freeze(state))


def write(root,bodies):
    for path,raw in bodies.items():
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)


def update(value,bodies,path,raw):
    bodies[path]=raw
    row=next(r for r in value['members'] if r['path']==path)
    row.update(bytes=len(raw),sha256=sha256(raw))


def repin(value,policy):
    policy.update(expected_aggregate_sha256=sha256(encode(value)),expected_members=copy.deepcopy(value['members']))
