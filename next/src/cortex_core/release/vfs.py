"""Bounded non-extracting v1 virtual inventory and selected-user footprint."""
import posixpath
from .artifacts import pyc_row
from .codec import decode_json, sha256
from .contracts import ReceiptRefusal


def require(condition,reason):
    if not condition:raise ReceiptRefusal(reason)


def parents(path):
    parts=path.split('/')
    return ['/'.join(parts[:n]) for n in range(1,len(parts))]


def access(record,uid,gid,bit):
    shift=6 if record['uid']==uid else 3 if record['gid']==gid else 0
    return bool((record['mode']>>shift)&bit)


def _record(record):
    return dict(record,kind=record.get('kind','regular'))


def inspect_profile_files(layers,blobs,config,value,profile,accepted,reader,admit_link,admit_generated,inspection=None):
    policy=profile['layer_policy'];base=policy['base'];runtime=profile['runtime']
    images=value['dependencies']['base_images']
    base_manifest=decode_json(accepted.get('deps/base-manifest.json',b''),8*1024*1024)
    base_config=decode_json(accepted.get('deps/base-config.json',b''),8*1024*1024)
    require(len(images)==1 and images[0]['manifest_digest']==base['manifest_digest']==sha256(accepted['deps/base-manifest.json']) and
            images[0]['config_digest']==base['config_digest']==sha256(accepted['deps/base-config.json']) and
            base_manifest['config']['digest']=='sha256:'+base['config_digest'] and
            base_config['architecture']==value['target']['oci_platform'].split('/')[1], 'base_manifest_identity')
    require(bool(layers) and layers[0]==base['layer_digest'] and sha256(accepted['deps/base-layer.tar'])==base['layer_digest'], 'base_layer_digest')
    all_rows=[];all_bodies=[];member_count=0;layer_bytes=0
    limits=value['resources']['disk_bytes']
    for key in layers:
        bodies,rows=reader(blobs[key],limits,metadata={'native_v1':True},layer_policy={'allow_directories':True,'symlinks':policy['symlinks']},preserve_order=True)
        member_count+=len(rows);layer_bytes+=len(blobs[key])
        require(member_count<=50000 and layer_bytes<=limits, 'virtual_inventory_bound')
        # Immutable deny is checked on every scanned member before folding;
        # a caller's inventory cannot turn OCI deletion markers into files.
        require(all(not component.startswith('.wh.') for r in rows for component in r['path'].split('/')), 'profile_whiteout_denied')
        all_rows.append([_record(r) for r in rows]);all_bodies.append(bodies)
    actual_base=all_rows[0]
    require(actual_base==base['inventory'] and decode_json(accepted['deps/base-inventory.json'],8*1024*1024)==base['inventory'], 'base_inventory_metadata')
    mapping=[];number=0
    for entry in config.get('history',[]):
        if entry.get('empty_layer') is True:mapping.append(None)
        else:mapping.append(number);number+=1
    require(mapping==profile['history_to_layers'] and number==len(layers), 'oci_history_layer_mapping')
    files={};contents={}
    roles=policy['layer_order']
    require(len(roles)==len(layers) and roles[0]=='base', 'layer_order_shape')
    for number,(rows,bodies) in enumerate(zip(all_rows,all_bodies)):
        role=roles[number]
        if number:
            admit_generated(all(not r['path'].startswith(('var/log/','var/cache/','tmp/','root/','home/')) and
                                r['path'] not in ('etc/passwd','etc/group','etc/shadow','etc/gshadow') for r in rows))
        for record in rows:
            path=record['path']
            if path in files:
                changes=[t for t in policy['transitions'] if t['layer']==role and t['path']==path]
                require(len(changes)==1 and changes[0]['before']==files[path] and changes[0]['after']==record, 'layer_transition_predecessor')
            files[path]=record
        contents.update(bodies)
    # Runtime-specific guards precede generic generated inventory mismatch, so
    # the selected footprint is causal and diagnostics remain precise.
    program=runtime['program'];resolved=runtime['resolved_program'];path=program.lstrip('/')
    require(program.startswith('/') and runtime['resolution']=='absolute' and config['config']['Entrypoint']==[program], 'runtime_program_resolution')
    if path in files and files[path]['kind']=='symlink':
        target=posixpath.normpath(posixpath.join(posixpath.dirname(path),files[path]['link_target']))
        admit_link(target==resolved.lstrip('/'))
    else:
        require(path==resolved.lstrip('/'),'runtime_resolution_target')
    executable=resolved.lstrip('/')
    require(executable in files and files[executable]['kind']=='regular', 'runtime_executable_missing')
    require(runtime['program_input'] in accepted and sha256(accepted[runtime['program_input']])==runtime['program_sha256'] and
            contents.get(executable)==accepted[runtime['program_input']], 'runtime_executable_digest')
    expected={r['path']:r for r in base['inventory']}
    for stage in policy['generated'].values():expected.update({r['path']:r for r in stage})
    require(executable in expected and all(files[executable][k]==expected[executable][k] for k in ('kind','mode','uid','gid','mtime')), 'runtime_executable_metadata')
    uid,gid=runtime['uid'],runtime['gid']
    require(config['config']['User']==str(uid)+':'+str(gid) and uid>0 and gid>0,'runtime_user_identity')
    require(all(p in files and files[p]['kind']=='directory' and access(files[p],uid,gid,1) for p in set(parents(executable)+parents(path))), 'runtime_traversal_access')
    require(access(files[executable],uid,gid,1), 'runtime_execute_access')
    cmd=config['config']['Cmd']
    require(cmd==['--module',value['target']['module_id'],'--config','/app/config.json'] and set(runtime['cmd_inputs'])=={'/app/config.json'} and
            all(p.lstrip('/') in files and files[p.lstrip('/')]['kind']=='regular' and binding['input'] in accepted and
                sha256(accepted[binding['input']])==binding['sha256'] and contents.get(p.lstrip('/'))==accepted[binding['input']] for p,binding in runtime['cmd_inputs'].items()), 'runtime_cmd_binding')
    require(all(p.lstrip('/') in files and access(files[p.lstrip('/')],uid,gid,4) and
                all(parent in files and files[parent]['kind']=='directory' and access(files[parent],uid,gid,1)
                    for parent in parents(p.lstrip('/'))) for p in runtime['cmd_inputs']), 'runtime_cmd_access')
    working=runtime['working_dir'].lstrip('/')
    require(config['config']['WorkingDir']==runtime['working_dir'] and working in files and files[working]['kind']=='directory', 'runtime_working_directory')
    require(working in files and access(files[working],uid,gid,1) and all(p in files and files[p]['kind']=='directory' and access(files[p],uid,gid,1) for p in parents(working)), 'runtime_working_directory_access')
    for number,rows in enumerate(all_rows):
        role=roles[number]
        require([r['path'] for r in rows]==policy['member_order'][role], 'layer_member_order')
        if number:
            require(rows==policy['generated'][role], 'generated_inventory_metadata')
    for path in files:
        require(all(p in files and files[p]['kind']=='directory' for p in parents(path)), 'virtual_parent_namespace')
    for retained,record in files.items():
        if record['kind']=='regular' and retained.endswith('.py'):
            require(retained in profile['source_bindings'], 'retained_source_unbound')
    for final,binding in profile['source_bindings'].items():
        require(final in files and files[final]['kind']=='regular' and binding['input'] in accepted and contents.get(final)==accepted[binding['input']], 'accepted_source_origin')
    caches=[p for p in sorted(files) if p.endswith('.pyc')]
    if profile['inspector']['python_caches']=='none':
        require(not caches, 'profile_cache_inventory')
    else:
        # CHECKED_HASH: only the session admit_inspection returned may judge caches;
        # the controller reads headers/names/source hashes, never marshal/compile.
        require(inspection is not None, 'native_inspector_execution_unqualified')
        require(all(files[p]['kind']=='regular' for p in caches), 'profile_cache_inventory')
        inspection.check([pyc_row(p,contents[p],contents,value) for p in caches])
    return [files[p] for p in sorted(files)]
