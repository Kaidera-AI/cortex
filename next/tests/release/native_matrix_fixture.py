"""Inert v1-profile receipt producer, never an admission oracle or executable.

Opaque programs, static accounts, mixed base metadata and generated transitions
are locally supplied public bytes. All transports/evidence are rebuilt coherently.
Only the existing R01 public API/CLI decides acceptance.
"""
import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import contextlib

from fixture import build_input, canonical, digest, file
from receipt_fixture import sha, tar, CLASSES, PHASES
from test_unit1_red import product

INDEX='application/vnd.oci.image.index.v1+json'
MANIFEST='application/vnd.oci.image.manifest.v1+json'
CONFIG='application/vnd.oci.image.config.v1+json'
LAYER='application/vnd.oci.image.layer.v1.tar'


def row(path, body=b'', *, kind='regular', uid=1001, gid=1001, mode=420, epoch=0, target=None):
    value=dict(path=path,kind=kind,uid=uid,gid=gid,mode=mode,mtime=epoch,body=body)
    if target is not None:value['link_target']=target
    return value


def inventory(entries):
    rows=[]
    for item in entries:
        body=item['link_target'].encode() if item['kind']=='symlink' else item['body']
        rows.append(dict(path=item['path'],kind=item['kind'],uid=item['uid'],gid=item['gid'],mode=item['mode'],
                         mtime=item['mtime'],bytes=len(body),sha256=sha(body),
                         **({'link_target':item['link_target']} if item['kind']=='symlink' else {})))
    return rows


def pack(entries):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w',format=tarfile.USTAR_FORMAT) as archive:
        for item in entries:
            member=tarfile.TarInfo(item['path']);member.uid=item['uid'];member.gid=item['gid'];member.mode=item['mode'];member.mtime=item['mtime']
            if item['kind']=='directory':member.type=tarfile.DIRTYPE
            elif item['kind']=='symlink':member.type=tarfile.SYMTYPE;member.linkname=item['link_target']
            member.size=len(item['body']);archive.addfile(member,io.BytesIO(item['body']))
    return stream.getvalue()


def desc(media,raw):return dict(mediaType=media,digest='sha256:'+sha(raw),size=len(raw))


def family(module='api',arch='amd64', *, fault=None):
    value,inputs=build_input();value['target']['module_id']=module;value['target']['oci_platform']='linux/'+arch;value['builder_requirements']['architecture']=arch
    for path,raw in list(inputs.items()):
        if path.startswith('contracts/'):
            obj=json.loads(raw)
            for key in ('module_id','producer_module_id'):
                if key in obj:obj[key]=module
            inputs[path]=canonical(obj)
    program=b'opaque-inert-program:'+module.encode()+b':'+arch.encode()+b'\n'
    base_program=b'opaque-inert-base-program:'+arch.encode()+b'\n';account=b'kos:x:1001:1001:fixture:/nonexistent:/sbin/nologin\n';shadow=b'kos:!:0:0:99999:7:::\n'
    base=[row('usr',kind='directory',uid=0,gid=0,mode=493,epoch=42),row('usr/bin',kind='directory',uid=0,gid=0,mode=493,epoch=42),
          row('usr/bin/base-runtime',base_program,uid=0,gid=0,mode=493,epoch=42),row('usr/lib',kind='directory',uid=0,gid=0,mode=493,epoch=42),row('usr/lib/libfixture.so',b'opaque-inert-library\n',uid=0,gid=0,epoch=42),row('etc',kind='directory',uid=0,gid=0,mode=493,epoch=42),
          row('etc/passwd',account,uid=0,gid=0,epoch=42),row('etc/shadow',shadow,uid=0,gid=0,mode=384,epoch=42),
          row('app',kind='directory',uid=0,gid=0,mode=493,epoch=42),row('app/state',b'base\n',uid=0,gid=0,epoch=42)]
    config_input=canonical({'module':module,'enabled':True})
    first=[row('app',kind='directory',mode=493),row('app/bin',kind='directory',mode=493),row('app/bin/cortex-real',program,mode=493),
           row('app/bin/cortex',kind='symlink',mode=511,target='cortex-real'),row('app/module.py',b'pass\n'),row('app/config.json',config_input),row('app/work',kind='directory',mode=493),row('app/state',b'one\n')]
    second=[row('app/state',b'two\n')]
    base_layer=pack(base);base_config=canonical(dict(os='linux',architecture=arch,config={'User':'1001:1001'},rootfs={'type':'layers','diff_ids':['sha256:'+sha(base_layer)]}))
    base_manifest=canonical(dict(schemaVersion=2,mediaType=MANIFEST,config=desc(CONFIG,base_config),layers=[desc(LAYER,base_layer)]))
    history=[{'created':'1970-01-01T00:00:42Z','created_by':'fixture-base'},
             {'created':'1970-01-01T00:00:00Z','created_by':'fixture-one'},
             {'created':'1970-01-01T00:00:00Z','created_by':'fixture-metadata','empty_layer':True},
             {'created':'1970-01-01T00:00:00Z','created_by':'fixture-two'}]
    runtime={'User':'1001:1001','Entrypoint':['/app/bin/cortex'],'Cmd':['--module',module,'--config','/app/config.json'],
             'WorkingDir':'/app/work','Labels':{'org.kaidera.module':module,'org.kaidera.version':'0.1.020'}}
    transitions=[{'layer':'one','path':'app','before':inventory([base[-2]])[0],'after':inventory([first[0]])[0]},
                 {'layer':'one','path':'app/state','before':inventory([base[-1]])[0],'after':inventory([first[-1]])[0]},
                 {'layer':'two','path':'app/state','before':inventory([first[-1]])[0],'after':inventory(second)[0]}]
    profile={'schema':'cortex.release-policy.v1','format_version':1,'target':copy.deepcopy(value['target']),
             'oci_config':copy.deepcopy(runtime),'history':copy.deepcopy(history),'history_to_layers':[0,1,None,2],
             'layer_policy':{'base':{'manifest_digest':sha(base_manifest),'config_digest':sha(base_config),'layer_digest':sha(base_layer),'inventory':inventory(base)},
                'generated':{'one':inventory(first),'two':inventory(second)},'layer_order':['base','one','two'],
                'member_order':{'base':[r['path'] for r in base],'one':[r['path'] for r in first],'two':[r['path'] for r in second]},
                'transitions':transitions,'symlinks':{'app/bin/cortex':'cortex-real'},'hardlinks':'deny','whiteouts':'deny','xattrs':'deny','acls':'deny'},
             'runtime':{'program':'/app/bin/cortex','resolved_program':'/app/bin/cortex-real','program_input':'deps/program.bin',
                        'program_sha256':sha(program),'resolution':'absolute','cmd_inputs':{'/app/config.json':{'input':'deps/runtime-config.json','sha256':sha(config_input)}},
                        'working_dir':'/app/work','uid':1001,'gid':1001},
             'source_bindings':{'app/module.py':{'input':'source/module.py','sha256':sha(b'pass\n'),'transform':'identity'}},
             'inspector':{'scope':'fixture','execution':'none','python_caches':'none'}}
    # Every profile fault is one syntactically coherent but unusable relation;
    # artifact faults hold the accepted input/profile fixed.
    if fault=='base_runtime_selected':
        runtime['Entrypoint']=['/usr/bin/base-runtime'];profile['oci_config']['Entrypoint']=copy.deepcopy(runtime['Entrypoint'])
        profile['runtime'].update(program='/usr/bin/base-runtime',resolved_program='/usr/bin/base-runtime',program_input='deps/base-program.bin',program_sha256=sha(base_program))
    if fault=='selected_uid_traversal':
        runtime['User']='2002:2002';profile['oci_config']['User']=runtime['User'];profile['runtime']['uid']=profile['runtime']['gid']=2002
        first[0]['mode']=448;profile['layer_policy']['generated']['one'][0]['mode']=448;profile['layer_policy']['transitions'][0]['after']['mode']=448
    if fault=='selected_uid_execute':
        runtime['User']='2002:2002';profile['oci_config']['User']=runtime['User'];profile['runtime']['uid']=profile['runtime']['gid']=2002
        first[2]['mode']=448;profile['layer_policy']['generated']['one'][2]['mode']=448
    if fault=='relative_program':
        runtime['Entrypoint']=['cortex'];profile['oci_config']['Entrypoint']=['cortex'];profile['runtime']['program']='cortex'
    if fault=='cmd_wrong_recipe':
        runtime['Cmd']=['--module','foreign','--config','/app/config.json'];profile['oci_config']['Cmd']=copy.deepcopy(runtime['Cmd'])
    if fault=='working_dir_inaccessible':
        first[6]['uid']=first[6]['gid']=2002;first[6]['mode']=448
        profile['layer_policy']['generated']['one'][6]=inventory([first[6]])[0]
    if fault=='base_inventory_metadata':profile['layer_policy']['base']['inventory'][2]['gid']=999
    if fault=='transition_predecessor':profile['layer_policy']['transitions'][-1]['before']['sha256']=sha(b'not-one\n')
    if fault=='empty_layer_position':
        history[1]['empty_layer']=True;history[2].pop('empty_layer');profile['history']=copy.deepcopy(history)
    # Materialize independently measured input records before modifying candidate.
    additions={'deps/program.bin':program,'deps/base-program.bin':base_program,'deps/runtime-config.json':config_input,'deps/base-layer.tar':base_layer,
               'deps/base-config.json':base_config,'deps/base-manifest.json':base_manifest,'deps/base-inventory.json':canonical(inventory(base)),
               'deps/base-provenance.json':canonical({'manifest_digest':sha(base_manifest),'config_digest':sha(base_config),'inventory_sha256':digest(inventory(base))})}
    inputs.update(additions);value['dependencies']['assets']=[file(k,v) for k,v in sorted(additions.items()) if k!='deps/base-provenance.json']
    value['dependencies']['base_images']=[{'role':'runtime','reference':'fixture/base@sha256:'+sha(base_manifest),'platform':'linux/'+arch,
        'manifest_digest':sha(base_manifest),'config_digest':sha(base_config),'provenance':file('deps/base-provenance.json',inputs['deps/base-provenance.json'])}]
    if fault=='base_manifest_identity':
        value['dependencies']['base_images'][0]['manifest_digest']='f'*64
        value['dependencies']['base_images'][0]['reference']='fixture/base@sha256:'+'f'*64
    descriptor=json.loads(inputs[value['descriptor']['path']]);descriptor['release_policy']=profile;inputs[value['descriptor']['path']]=canonical(descriptor)
    for key in ('descriptor',):value[key]=file(value[key]['path'],inputs[value[key]['path']])
    for key in ('compatibility_contract','schema_manifest','api_descriptor','event_index'):
        path=value['compatibility_inputs'][key]['path'];value['compatibility_inputs'][key]=file(path,inputs[path])
    value['compatibility_inputs']['event_schemas']=[file(r['path'],inputs[r['path']]) for r in value['compatibility_inputs']['event_schemas']]
    policy_sha=digest(profile);value['admission_policy']['policy_sha256']=policy_sha
    # The actual generated bytes are changed only after source/profile acceptance.
    candidate_base=copy.deepcopy(base);candidate_first=copy.deepcopy(first);candidate_second=copy.deepcopy(second)
    if fault=='executable_missing':candidate_first=[r for r in candidate_first if r['path']!='app/bin/cortex-real']
    if fault=='executable_bytes':candidate_first[2]['body']=b'changed-inert-program\n'
    if fault=='executable_metadata':candidate_first[2]['mode']=420
    if fault=='unsafe_link':candidate_first[3]['link_target']='../../outside'
    if fault=='cmd_input_missing':candidate_first=[r for r in candidate_first if r['path']!='app/config.json']
    if fault=='working_dir_missing':candidate_first=[r for r in candidate_first if r['path']!='app/work']
    if fault=='working_dir_file':
        item=next(r for r in candidate_first if r['path']=='app/work');item['kind']='regular';item['body']=b'not-directory\n';item['mode']=420
    if fault=='base_content':candidate_base[2]['body']=b'changed-inert-base\n'
    if fault=='static_account':next(r for r in candidate_base if r['path']=='etc/passwd')['body']=b'changed-synthetic-account\n'
    if fault=='generated_account':candidate_second.append(row('etc/passwd',b'synthetic-unapproved\n'))
    if fault=='member_order':candidate_first[4],candidate_first[5]=candidate_first[5],candidate_first[4]
    if fault=='history':history[-1]['created_by']='unapproved-other'
    layers=[pack(candidate_base),pack(candidate_first),pack(candidate_second)];entry_layers=[candidate_base,candidate_first,candidate_second]
    if fault=='layer_order':layers[1],layers[2]=layers[2],layers[1];entry_layers[1],entry_layers[2]=entry_layers[2],entry_layers[1]
    if fault=='cross_platform_base':
        # A coherent other-platform base descriptor is still not this accepted base.
        candidate_base[2]['body']=b'opaque-inert-base-program:'+('arm64' if arch=='amd64' else 'amd64').encode()+b'\n';layers[0]=pack(candidate_base)
    config=canonical({'architecture':arch,'os':'linux','created':'1970-01-01T00:00:00Z','config':runtime,'history':history,
                      'rootfs':{'type':'layers','diff_ids':['sha256:'+sha(r) for r in layers]}})
    manifest=canonical({'schemaVersion':2,'mediaType':MANIFEST,'config':desc(CONFIG,config),'layers':[desc(LAYER,r) for r in layers]})
    index=canonical({'schemaVersion':2,'mediaType':INDEX,'manifests':[dict(desc(MANIFEST,manifest),platform={'os':'linux','architecture':arch})]})
    blobs={sha(r):r for r in [manifest,config,*layers]};archive=tar({'oci-layout':canonical({'imageLayoutVersion':'1.0.0'}),'index.json':index,
                       **{'blobs/sha256/'+k:r for k,r in blobs.items()}})
    final={}
    for entries in entry_layers:
        for record in inventory(entries):final[record['path']]=record
    final_inventory=[final[k] for k in sorted(final)];inventory_sha=digest(final_inventory)
    outputs={};evidence={};builds={};reports={};all_evidence=[]
    def artifact(storage,role,path,raw,media='application/json'):
        storage[path]=raw;return dict(role=role,path=path,media_type=media,bytes=len(raw),sha256=sha(raw))
    for slot in ('A','B'):
        identity='fixture-'+module+'-'+arch+'-'+slot
        observed=dict(qualification_scope='fixture',os='linux',architecture=arch,kernel='inert-fixture',uid=1001,rootless=True,
                      toolchain=value['builder_requirements']['toolchain'],engine=value['builder_requirements']['engine'],fresh_store=True,mutable_shared=False,
                      **{k:identity+'-'+k for k in ('build_id','execution_id','store_id','runroot_id','container_id','source_id')});observed['build_id']=identity
        operations=[]
        for number,phase in enumerate(PHASES):
            op=dict(phase=phase,argv=value['recipe']['argv_template'] if phase=='build' else ['fixture',phase],environment_names=['PYTHONHASHSEED'],
                    environment_policy_sha256=policy_sha,status='exited',exit_code=0,started_at_unix_ns=number*1000,finished_at_unix_ns=(number+1)*1000)
            ev=artifact(evidence,slot+'.operation.'+phase,'evidence/'+slot+'/'+phase+'.json',canonical(dict(op,build_id=identity,qualification_scope='fixture')))
            operations.append(dict(op,evidence=ev));all_evidence.append(ev)
        raw_outputs={'archive':('application/x-tar',archive),'index':(INDEX,index),'manifest':(MANIFEST,manifest),'config':(CONFIG,config),
                     **{'blob.'+sha(r):('application/octet-stream',r) for r in layers}}
        records=[artifact(outputs,role,slot+'/'+role+'.bin',raw,media) for role,(media,raw) in raw_outputs.items()]
        provenance=dict(build_id=identity,input_sha256=digest(value),output_sha256=sha(archive),source_sha256=digest(value['source']),
                        recipe_sha256=digest(value['recipe']),dependency_sha256=digest(value['dependencies']),policy_sha256=policy_sha,
                        observed_builder=observed,qualification_scope='fixture')
        provenance_record=artifact(outputs,'provenance',slot+'/provenance.json',canonical(provenance));records.extend([provenance_record,artifact(outputs,'sbom',slot+'/sbom.json',canonical(dict(output_sha256=sha(archive),inventory_sha256=inventory_sha)))])
        records.sort(key=lambda r:r['path']);slot_reports={}
        for category in CLASSES:
            ev=artifact(evidence,slot+'.audit.'+category,'evidence/'+slot+'/audit-'+category+'.json',canonical(dict(build_id=identity,**{'class':category},output_sha256=sha(archive),inventory_sha256=inventory_sha,findings=[])));slot_reports[category]=ev;all_evidence.append(ev)
        custody=artifact(evidence,slot+'.custody','evidence/'+slot+'/custody.json',canonical(dict(build_id=identity,outputs=records,complete=True)))
        cleanup=artifact(evidence,slot+'.cleanup','evidence/'+slot+'/cleanup-result.json',canonical(dict(build_id=identity,removed_resources=[observed[k] for k in ('execution_id','store_id','runroot_id','container_id','source_id')],remaining_owned_resources=[],complete=True)))
        all_evidence.extend([custody,cleanup]);reports[slot]=slot_reports
        builds[slot]=dict(build_id=identity,observed_builder=observed,source_sha256=digest(value['source']),recipe_sha256=digest(value['recipe']),dependency_sha256=digest(value['dependencies']),
            started_at_unix_ns=0,finished_at_unix_ns=6000,operations=operations,outputs=records,oci=dict(index=sha(index),manifest=sha(manifest),config=sha(config),blobs=sorted(blobs),platform='linux/'+arch),provenance=provenance_record,custody=custody,cleanup=cleanup)
    cleanup=artifact(evidence,'cleanup','evidence/cleanup.json',canonical(dict(build_ids=[builds[s]['build_id'] for s in ('A','B')],complete=True,remaining_owned_resources=[])))
    custody=artifact(evidence,'custody','evidence/custody.json',canonical(dict(complete=True,partial=False,missing_roles=[],outputs=sorted([r for s in ('A','B') for r in builds[s]['outputs']],key=lambda r:r['path']))))
    all_evidence.extend([cleanup,custody]);all_evidence.sort(key=lambda r:r['path']);ev_index=artifact(evidence,'evidence-index','evidence/index.json',canonical(dict(inventory=all_evidence)))
    receipt=dict(schema='cortex.build-receipt.v1',format_version=1,input_sha256=digest(value),target=value['target'],qualification_scope='fixture',result='PASS',reason='complete',builds=builds,
        comparison=dict(raw_archives_equal=True,raw_archive_sha256=sha(archive),raw_archive_bytes=len(archive),oci_closure_equal=True,semantic_audit_equal=True),
        audit=dict(policy_id='fixture-policy',policy_sha256=policy_sha,required_classes=CLASSES,reports=reports,findings=[]),evidence=dict(inventory=all_evidence,index=ev_index),
        custody=dict(complete=True,partial=False,missing_roles=[],receipt=custody),cleanup=dict(complete=True,receipt=cleanup,remaining_owned_resources=[]))
    acceptance=dict(input_sha256=digest(value),policy_sha256=policy_sha,qualification_scope='fixture',required_classes=CLASSES)
    return value,inputs,receipt,outputs,evidence,acceptance


def api(case):
    value,inputs,receipt,outputs,evidence,acceptance=case
    owner=product('inputs');admitted=owner.admit_build_input(owner.parse_build_input(canonical(value)),inputs.__getitem__,acceptance)
    owner=product('receipts');verified=owner.verify_build_receipt(owner.parse_build_receipt(canonical(receipt)),admitted,outputs.__getitem__,evidence.__getitem__,acceptance)
    return {'decision':'verified','receipt_sha256':verified.receipt_sha256,'input_sha256':verified.input_sha256,'output_sha256':verified.output_sha256,'native_qualified':verified.native_qualified}


def cli(case):
    from cortex_core.cli import release
    value,inputs,receipt,outputs,evidence,acceptance=case
    with tempfile.TemporaryDirectory(prefix='r01-matrix-cli-') as directory:
        root=Path(directory)
        for name,storage in [('inputs',inputs),('outputs',outputs),('evidence',evidence)]:
            for path,raw in storage.items():
                f=root/name/path;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw);f.chmod(420)
        for name,obj in [('input',value),('receipt',receipt),('policy',acceptance)]: (root/(name+'.json')).write_bytes(canonical(obj))
        argv=['verify-build-receipt','--input',str(root/'input.json'),'--input-sha256',digest(value),'--input-root',str(root/'inputs'),
              '--policy',str(root/'policy.json'),'--policy-record-sha256',digest(acceptance),'--receipt',str(root/'receipt.json'),'--receipt-sha256',digest(receipt),
              '--output-root',str(root/'outputs'),'--evidence-root',str(root/'evidence'),'--decision',str(root/'decision.json')]
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):code=release.main(argv)
        result=json.loads(capture.getvalue())
        if code==2:raise ValueError(result['reason'])
        if code!=0:raise AssertionError('unexpected CLI exit '+str(code))
        return result
