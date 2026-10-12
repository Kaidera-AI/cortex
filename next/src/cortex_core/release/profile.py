"""One closed R01 descriptor profile; independently accepted input owns identity."""
import posixpath
import re
from .codec import canonical_bytes, relative_path, schema_validate, sha256
from .contracts import BuildRefusal


def require(condition,reason):
    if not condition:raise BuildRefusal(reason)


def _runtime_config(config):
    require(isinstance(config,dict) and set(config)<= {'User','Env','Entrypoint','Cmd','WorkingDir','Labels','ExposedPorts','Volumes','StopSignal'},'unreviewed_oci_config_field')
    if 'User' in config:
        user=config['User']
        require(isinstance(user,str) and re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*',user) is not None,'runtime_nonroot_user')
    for name in ('Entrypoint','Cmd','Env'):
        if name in config:
            require(isinstance(config[name],list) and len(config[name])<=256 and all(isinstance(v,str) and len(v.encode())<=4096 for v in config[name]),'oci_config_argument_bound')
    if 'Entrypoint' in config:
        argv=config['Entrypoint']
        require(bool(argv) and posixpath.basename(argv[0]) not in ('sh','bash','dash','zsh','ksh','env'),'runtime_literal_entrypoint')
    env=config.get('Env',[])
    names=[v.split('=',1)[0] for v in env]
    require(all('=' in v and re.fullmatch(r'[A-Z][A-Z0-9_]*',name) for name,v in zip(names,env)) and len(names)==len(set(names)) and
            not any(re.search(r'(?:TOKEN|PASSWORD|SECRET|DSN|(?:^|_)KEY(?:_|$))',name) for name in names),'runtime_public_environment')


def admit_profile(profile,value,actual):
    require(isinstance(profile,dict),'invalid_release_policy')
    if 'schema' not in profile:
        require(set(profile)=={'oci_config','history','layer_policy'} and isinstance(profile['history'],list),'invalid_release_policy')
        layer=profile['layer_policy']
        require(isinstance(layer,dict) and set(layer)=={'allow_replacements','allow_directories','symlinks'} and type(layer['allow_replacements']) is bool and type(layer['allow_directories']) is bool and isinstance(layer['symlinks'],dict),'invalid_layer_policy')
        for path,destination in layer['symlinks'].items():
            relative_path(path);require(isinstance(destination,str) and not destination.startswith('/') and '\\' not in destination,'invalid_layer_symlink')
            relative_path(posixpath.normpath(posixpath.join(posixpath.dirname(path),destination)))
        for entry in profile['history']:
            require(isinstance(entry,dict) and set(entry)<= {'created_by','comment','empty_layer'} and all(isinstance(v,str) if k!='empty_layer' else type(v) is bool for k,v in entry.items()),'invalid_history_policy')
    else:
        schema_validate(profile,'release-policy.schema.json')
        require(profile['target']==value['target'],'profile_target_identity')
        require(sha256(canonical_bytes(profile))==value['admission_policy']['policy_sha256'],'profile_policy_identity')
        # Source bytes are retained from measured input, never supplied by output.
        for final,binding in profile['source_bindings'].items():
            relative_path(final);relative_path(binding['input'])
            require(binding['transform']=='identity' and binding['input'] in actual and sha256(actual[binding['input']])==binding['sha256'],'accepted_source_binding')
    _runtime_config(profile['oci_config'])
    return profile
