"""Measured original aggregate data, without signing/native/effect capability.

Reuses the sole R01 input/receipt/OCI and RC0 parsers. Mapping is owner-internal,
never artifact authority. Fixture success is unsigned data only. Real current
originals, secondary-output representation and installed composition stay held.
"""
from .codec import decode_json,sha256
from .contracts import freeze,thaw,BuildRefusal,ReceiptRefusal
from .custody import RootReader
from .inputs import parse_build_input,admit_build_input,input_records
from .receipts import parse_build_receipt,verify_build_receipt
from .artifacts import tar_members
from .trust import _require,_depth,_MANIFEST_VALIDATOR
from cortex_core.runtime.contracts import load_contracts,decode,RC0_SHA256

# PROVISIONAL pending Cox 1eb2ba12. This is the sole U3-C hard-cap table;
# independent owner policy can tighten it. No schema/old oracle bytes are changed.
PROVISIONAL_LIMITS=freeze(dict(image_bytes=768*1024**2,record_bytes=8*1024**2,
    aggregate_bytes=16*1024**3,manifest_bytes=1048576,signature_bytes=4096,
    lock_bytes=262144,member_count=10000,depth=32,read_seconds=120))
_PAIRS={(m+'-image','linux/'+a) for m in ('core','api') for a in ('arm64','amd64')}


def _limits(policy):
    limits=policy.get('limits')
    _require(type(limits) is dict and set(limits)==set(PROVISIONAL_LIMITS)
             and all(type(v) is int and v>0 for v in limits.values()),'required_binding_gap')
    return {key:min(value,limits[key]) for key,value in PROVISIONAL_LIMITS.items()}


def _validate_manifest(raw,policy):
    limits=_limits(policy)
    _require(type(raw) is bytes and len(raw)<=limits['manifest_bytes'],'raw_limit')
    try:value=decode_json(raw,limits['manifest_bytes'])
    except (ValueError,RecursionError,OverflowError):
        from cortex_core.runtime.signed_member import Refusal
        raise Refusal('aggregate_shape') from None
    _depth(value,limits['depth'])
    _require(type(value) is dict and type(value.get('members')) is list,'aggregate_shape')
    rows=value['members']
    _require(len(rows)<=limits['member_count'],'raw_limit')
    _require(all(type(r) is dict and type(r.get('path')) is str for r in rows),'aggregate_shape')
    paths=[r['path'] for r in rows]
    _require(paths==sorted(set(paths)),'aggregate_shape')
    images=[r for r in rows if r.get('kind')=='image']
    _require(len(images)==4 and {(r.get('role'),r.get('target')) for r in images}==_PAIRS,'aggregate_pair_mismatch')
    _require(next(_MANIFEST_VALIDATOR.iter_errors(value),None) is None,'aggregate_shape')
    _require(rows==policy['expected_members'],'aggregate_pair_mismatch')
    return value,limits


def _measure_members(value,reader,limits,*,overhead_bytes=0):
    for row in value['members']:RootReader._aggregate_path(row['path'])
    reader.aggregate_begin(value['members'],limits,overhead_bytes=overhead_bytes)
    bodies={}
    for row in value['members']:
        raw=reader.aggregate_bytes(row['path'])
        _require(len(raw)==row['bytes'] and sha256(raw)==row['sha256'],'member_digest_mismatch')
        bodies[row['path']]=raw
    return bodies


def _json(raw):
    try:return decode_json(raw,PROVISIONAL_LIMITS['record_bytes'],canonical=False)
    except (ValueError,RecursionError,OverflowError):
        from cortex_core.runtime.signed_member import Refusal
        raise Refusal('aggregate_pair_mismatch') from None


def _no_embedded_lock(image,lock,limits,*,metadata=None):
    # The existing bounded tar/JSON parsers measure original layer bodies;
    # no extraction, decompression, second OCI implementation or execution.
    outer,_=tar_members(image,limits['image_bytes'])
    index=_json(outer['index.json'])
    for reference in index['manifests']:
        manifest=_json(outer['blobs/sha256/'+reference['digest'].removeprefix('sha256:')])
        for layer in manifest['layers']:
            members,_=tar_members(outer['blobs/sha256/'+layer['digest'].removeprefix('sha256:')],limits['image_bytes'],
                metadata=None if metadata is None else {'native_v1':True},
                layer_policy=None if metadata is None else {'allow_directories':True,'symlinks':metadata['layer_policy']['symlinks']})
            for path,raw in members.items():
                _require(path.split('/')[-1]!='runtime.lock.json','member_path_alias')
                _require(not (len(raw)==len(lock) and sha256(raw)==sha256(lock)),'member_path_alias')


def _builds(value,bodies,builds):
    rows={r['path']:r for r in value['members']};images=[r for r in rows.values() if r['kind']=='image']
    _require(type(builds) is list and len(builds)==4,'required_binding_gap')
    _require(all(type(g) is dict and set(g)=={'image','input','receipt','paths','policy'} for g in builds),'required_binding_gap')
    _require({g['image'] for g in builds}=={r['path'] for r in images},'aggregate_pair_mismatch')
    admitted=[];descriptor_paths=[]
    for group in builds:
        image=rows[group['image']];mapping=group['paths'];policy=group['policy']
        _require(type(mapping) is dict and type(policy) is dict,'required_binding_gap')
        _require(group['input'] in bodies and group['receipt'] in bodies,'required_binding_gap')
        _require(rows[group['input']]['role']=='build-input' and rows[group['receipt']]['role']=='build-receipt','aggregate_pair_mismatch')
        _require(sha256(bodies[group['input']])==image['build_input_sha256'],'aggregate_pair_mismatch')
        _require(sha256(bodies[group['receipt']])==image['build_receipt_sha256'],'aggregate_pair_mismatch')
        parsed=parse_build_input(bodies[group['input']]);receipt=parse_build_receipt(bodies[group['receipt']])
        iv=thaw(parsed.document);rv=thaw(receipt.document)
        native=value['purpose']=='cortex/core-api/production'
        _require(not native or (iv['admission_policy']['qualification_scope']=='native' and rv['qualification_scope']=='native'
                                and policy.get('qualification_scope')=='native'),'required_binding_gap')
        _require(iv['target']=={'module_id':image['module_id'],
                 'module_version':image['module_version'],'oci_platform':image['target']},'aggregate_pair_mismatch')
        _require(image['module_id']==image['role'].removesuffix('-image') and
                 image['module_version']==value['release'][image['module_id']+'_module_version'],'aggregate_pair_mismatch')
        _require(iv['source']['revision']==value['source']['commit'] and iv['source']['tree']==value['source']['tree'],'aggregate_pair_mismatch')
        inputs=input_records(iv);evidence=rv['evidence']['inventory']+[rv['evidence']['index']]
        outputs=[r for slot in ('A','B') for r in rv['builds'][slot]['outputs']]
        records=inputs+evidence+outputs
        originals={r['path'] for r in records}
        _require(originals<=set(mapping),'required_binding_gap')
        _require(set(mapping)==originals and len(set(mapping.values()))==len(mapping),'aggregate_pair_mismatch')
        _require(all(path in bodies for path in mapping.values()),'required_binding_gap')
        _require(mapping[rv['builds']['A']['outputs'][next(i for i,r in enumerate(rv['builds']['A']['outputs']) if r['role']=='archive')]['path']]==image['path'],'aggregate_pair_mismatch')
        typed={r['path']:('record','descriptor' if r['path']==iv['descriptor']['path'] else 'evidence') for r in inputs}
        typed.update({r['path']:('record','evidence') for r in evidence})
        typed.update({r['path']:('image',image['role']) if mapping[r['path']]==image['path'] else
                      ('record',r['role'] if r['role'] in ('sbom','provenance') else 'evidence') for r in outputs})
        _require(all((rows[mapping[path]]['kind'],rows[mapping[path]]['role'])==identity
                     for path,identity in typed.items()),'aggregate_pair_mismatch')
        def original(path):return bodies[mapping[path]]
        # Sole original R01 qualification path. A fixture never gains native
        # authority; current native input/inspector custody remains held there.
        vi=admit_build_input(parsed,original,policy)
        verify_build_receipt(receipt,vi,original,original,policy)
        # R01 already enforces native input qualification, measured originals,
        # equal A/B archives/closure and all receipt target/output subjects.
        _require(rv['builds']['A']['oci']['manifest']==image['oci_manifest_sha256'],'aggregate_pair_mismatch')
        descriptor_paths.append(mapping[iv['descriptor']['path']]);admitted.append(vi)
    _require(sorted(descriptor_paths)==value['compatibility']['descriptor_members'],'aggregate_pair_mismatch')
    return admitted


def _compatibility(value,bodies,admitted):
    refs=value['compatibility'];release=value['release']
    _require(refs['api_contract']==release['api_contract'] and refs['schema_version']==release['schema_version']
             and refs['event_version']==release['event_version'],'aggregate_pair_mismatch')
    roles={r['path']:r['role'] for r in value['members']}
    for key,role in (('compatibility_member','compatibility'),('schema_member','schema'),('event_member','events'),
                     ('migration_member','migrations'),('operations_member','operations')):
        _require(refs[key] in bodies and roles[refs[key]]==role,'aggregate_pair_mismatch')
    compatibility=_json(bodies[refs['compatibility_member']]);schema=_json(bodies[refs['schema_member']])
    events=_json(bodies[refs['event_member']]);migrations=_json(bodies[refs['migration_member']])
    _require(compatibility.get('api_contract')==release['api_contract'],'aggregate_pair_mismatch')
    _require(schema.get('version')==release['schema_version'],'aggregate_pair_mismatch')
    _require(events.get('version')==release['event_version'],'aggregate_pair_mismatch')
    _require(migrations.get('migrations')==schema.get('migrations'),'aggregate_pair_mismatch')
    for vi in admitted:
        metadata=thaw(vi.metadata);docs=metadata['compatibility']
        # The existing R01 metadata parser enforces closed descriptor/ABI and
        # required module/dependency tuples. Compare its actual original docs.
        _require(docs['compatibility_contract']==compatibility
                 and docs['schema_manifest']==schema and docs['event_index']['version']==events['version'],'aggregate_pair_mismatch')


def _lock(value,bodies):
    raw=bodies['runtime.lock.json'];lock=decode(raw,limit=PROVISIONAL_LIMITS['lock_bytes'])
    # RC0's historical SQL consts are not new native target acceptance. The
    # original schema remains pinned; actual current target originals are GAP.
    from cortex_core.runtime.config import _bindings,_image_lock_cycles
    try:_image_lock_cycles(lock)
    except ValueError as error:
        from cortex_core.runtime.signed_member import Refusal
        if isinstance(error,Refusal) and error.reason=='image_lock_cycle':
            raise Refusal('member_path_alias') from None
        raise
    _require(all(binding.get('state')=='BOUND' for binding in _bindings(lock)),'required_binding_gap')
    _require(lock['source']['value']['revision']==value['source']['commit'] and
             lock['source']['value']['tree']==value['source']['tree'],'aggregate_pair_mismatch')
    _require(lock['release']['value']==value['release'],'aggregate_pair_mismatch')
    for row in value['members']:
        if row['kind']=='image':
            component=lock['targets'][row['target']][row['module_id']]['image']['value']
            _require(component['sha256']==row['oci_manifest_sha256'] and component['version']==row['module_version']
                     and component['abi']==row['target'],'aggregate_pair_mismatch')
    for role,key,filename in (('lock-schema','lock_schema','runtime-lock-v1.schema.json'),
                              ('config-schema','config_schema','runtime-config-v1.schema.json'),
                              ('operations','operations','runtime-operations-v1.json')):
        records=[row for row in value['members'] if row['role']==role]
        _require(len(records)==1,'aggregate_pair_mismatch')
        record=records[0];body=bodies[record['path']]
        _require(sha256(body)==RC0_SHA256[filename],'aggregate_pair_mismatch')
        bound=lock['contract_members'][key]['value']
        _require(bound['sha256']==sha256(body) and bound['bytes']==len(body),'aggregate_pair_mismatch')
    contracts=load_contracts()
    _require(next(contracts.lock_validator.iter_errors(lock),None) is None,'aggregate_pair_mismatch')
    return raw


def _check_pairs(value,bodies,builds,limits):
    try:
        for row in value['members']:
            if row['media_type']=='application/json':_json(bodies[row['path']])
        admitted=_builds(value,bodies,builds)
        _compatibility(value,bodies,admitted)
        lock=_lock(value,bodies)
        for row in value['members']:
            if row['kind']=='image':
                vi=next(v for v in admitted if v.target['module_id']==row['module_id'] and v.target['oci_platform']==row['target'])
                _no_embedded_lock(bodies[row['path']],lock,limits,metadata=thaw(vi.metadata)['release_policy'])
    except (BuildRefusal,ReceiptRefusal,KeyError,TypeError,ValueError) as error:
        from cortex_core.runtime.signed_member import Refusal
        if isinstance(error,Refusal):raise
        raise Refusal('aggregate_pair_mismatch') from None
