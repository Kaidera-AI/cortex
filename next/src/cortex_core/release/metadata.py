"""Role-specific identities in already measured pre-build metadata files."""
import posixpath
import re
from .codec import decode_json, relative_path, sha256
from .contracts import BuildRefusal


def require(condition, reason):
    if not condition:
        raise BuildRefusal(reason)


def _object(raw):
    value = decode_json(raw, 1024 * 1024, canonical=False)
    require(isinstance(value, dict), 'metadata_object_required')
    return value


def _version(value, key='version'):
    require(type(value.get(key)) is int and value[key] == 1, 'unsupported_metadata_version')


def _id(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 128


def parse_metadata(value, actual, native):
    descriptor = _object(actual[value['descriptor']['path']])
    target = value['target']
    require(descriptor.get('module_id') == target['module_id'] and descriptor.get('module_version') == target['module_version'], 'descriptor_target_mismatch')
    _version(descriptor, 'interface_version')
    profile = descriptor.get('release_policy', {'oci_config': {}, 'history': [],
                'layer_policy': {'allow_replacements': False, 'allow_directories': False, 'symlinks': {}}})
    require(isinstance(profile, dict) and set(profile) == {'oci_config','history','layer_policy'} and
            isinstance(profile['oci_config'], dict) and isinstance(profile['history'], list), 'invalid_release_policy')
    layer = profile['layer_policy']
    require(isinstance(layer, dict) and set(layer) == {'allow_replacements','allow_directories','symlinks'} and
            type(layer['allow_replacements']) is bool and type(layer['allow_directories']) is bool and
            isinstance(layer['symlinks'], dict), 'invalid_layer_policy')
    for path, destination in layer['symlinks'].items():
        relative_path(path)
        require(isinstance(destination, str) and not destination.startswith('/') and '\\' not in destination,
                'invalid_layer_symlink')
        relative_path(posixpath.normpath(posixpath.join(posixpath.dirname(path), destination)))
    require(set(profile['oci_config']) <= {'User','Env','Entrypoint','Cmd','WorkingDir','Labels','ExposedPorts','Volumes','StopSignal'}, 'unreviewed_oci_config_field')
    for field in ('Entrypoint','Cmd','Env'):
        if field in profile['oci_config']:
            require(isinstance(profile['oci_config'][field], list) and len(profile['oci_config'][field]) <= 256 and
                    all(isinstance(s,str) and len(s.encode('utf-8')) <= 4096 for s in profile['oci_config'][field]), 'oci_config_argument_bound')
    for entry in profile['history']:
        require(isinstance(entry, dict) and set(entry) <= {'created_by','comment','empty_layer'} and
                all(isinstance(v, str) if k != 'empty_layer' else type(v) is bool for k,v in entry.items()), 'invalid_history_policy')
    records = value['compatibility_inputs']
    parsed = {name: _object(actual[records[name]['path']]) for name in
              ('compatibility_contract','schema_manifest','api_descriptor','event_index')}
    for document in parsed.values():
        _version(document)
    api = parsed['api_descriptor']
    require(api.get('module_id') == target['module_id'] and api.get('module_version') == target['module_version'] and
            api.get('interface_version') == descriptor['interface_version'] and type(api.get('interface_version')) is int,
            'api_descriptor_identity')
    event_index = parsed['event_index']
    _version(event_index, 'event_version')
    events = [_object(actual[row['path']]) for row in records['event_schemas']]
    for event in [event_index, *events]:
        _version(event)
        require(event.get('producer_module_id') == target['module_id'] and event.get('producer_module_version') == target['module_version'] and
                type(event.get('event_version')) is int and event['event_version'] == event_index['event_version'], 'event_producer_identity')
    interfaces = [_object(actual[row['path']]) for row in records['dependency_interfaces']]
    for interface in interfaces:
        _version(interface)
        _version(interface, 'interface_version')
        require(_id(interface.get('module_id')) and _id(interface.get('module_version')), 'dependency_interface_identity')
    manifest = parsed['schema_manifest']
    require(isinstance(manifest.get('migrations'), list) and len(manifest['migrations']) <= 20000, 'invalid_schema_manifest')
    seen = set()
    for migration in manifest['migrations']:
        require(isinstance(migration, dict) and set(migration) == {'id','file','sha256'} and _id(migration['id']) and migration['id'] not in seen and
                isinstance(migration['sha256'], str) and re.fullmatch('[0-9a-f]{64}', migration['sha256']), 'invalid_migration_record')
        path = posixpath.join(posixpath.dirname(records['schema_manifest']['path']), relative_path(migration['file']))
        require(path in actual and sha256(actual[path]) == migration['sha256'], 'unbound_migration_input')
        seen.add(migration['id'])
    modules = parsed['compatibility_contract'].get('modules')
    if native or modules is not None:
        require(isinstance(modules, list) and 1 <= len(modules) <= 20000, 'compatibility_modules_required')
        required = {target['module_id']: (target['module_version'], descriptor['interface_version'])}
        required.update({doc['module_id']: (doc['module_version'], doc['interface_version']) for doc in interfaces})
        admitted = {}
        for module in modules:
            require(isinstance(module, dict) and set(module) == {'module_id','module_version','interface_version','schema_version','event_version'} and
                    _id(module['module_id']) and _id(module['module_version']) and module['module_id'] not in admitted,
                    'invalid_compatibility_module')
            for key in ('interface_version','schema_version','event_version'):
                _version(module,key)
            admitted[module['module_id']] = (module['module_version'],module['interface_version'])
        require(all(admitted.get(key) == item for key,item in required.items()), 'unsupported_module_tuple')
    return {'descriptor': descriptor, 'compatibility': parsed, 'event_schemas': events,
            'dependency_interfaces': interfaces, 'release_policy': profile}
