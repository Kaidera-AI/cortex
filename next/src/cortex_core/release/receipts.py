"""Shared build receipt verifier; no assembly, signing or installation effects."""
from dataclasses import dataclass
from .artifacts import inspect_oci, measure, require
from .codec import canonical_bytes, decode_json, ordered_inventory, schema_validate, sha256
from .contracts import ParsedBuildReceipt, ReceiptRefusal, VerifiedBuildInput, VerifiedBuildReceipt, _SEAL, freeze, thaw

CLASSES = ('accounts', 'cache', 'clock', 'files', 'input', 'labels', 'logs', 'python', 'runtime', 'serialization', 'xattrs-acls')
PHASES = ('input', 'parser', 'build', 'export', 'audit', 'cleanup')
IDENTITIES = ('build_id', 'execution_id', 'store_id', 'runroot_id', 'container_id', 'source_id')


QUALIFICATION_SCHEMA = 'cortex.native-qualification.v1'
QUALIFICATION_KEYS = {'schema', 'architecture', 'platform', 'host', 'engine', 'engine_path', 'engine_environment',
                      'engine_config', 'inspector', 'containment'}
PROBES = ('memory', 'mounts_env', 'network', 'pids', 'rootfs_write', 'time', 'tmpfs')


@dataclass(frozen=True)
class MatchedQualification:
    """R01's statement that measured R6 records matched the accepted context."""
    closure_sha256: str
    architecture: str
    _seal: object


def _hex(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def qualification_complete(doc):
    """The ONE closed R6 qualification record shape; every result must be a pass.

    Producer completeness (emit) and R01's match both use this check; it is not
    independent authority by itself.
    """
    def run(row):
        return (type(row) is dict and set(row) == {'status', 'request_sha256', 'reply_sha256'} and
                row['status'] == 'pass' and _hex(row['request_sha256']) and _hex(row['reply_sha256']))
    try:
        valid = (type(doc) is dict and set(doc) == QUALIFICATION_KEYS and doc['schema'] == QUALIFICATION_SCHEMA and
                 doc['architecture'] in ('amd64', 'arm64') and doc['platform'] == 'linux/' + doc['architecture'] and
                 type(doc['host']) is dict and set(doc['host']) == {'os', 'arch', 'uid', 'rootless', 'emulated', 'kernel'} and
                 doc['host']['os'] == 'linux' and doc['host']['rootless'] is True and doc['host']['emulated'] is False and
                 type(doc['host']['uid']) is int and doc['host']['uid'] > 0 and
                 type(doc['engine']) is dict and set(doc['engine']) == {'engine_id', 'version', 'sha256'} and
                 _hex(doc['engine']['sha256']) and type(doc['engine_path']) is str and doc['engine_path'].startswith('/') and
                 type(doc['engine_environment']) is list and doc['engine_environment'] == sorted(set(doc['engine_environment'])) and
                 doc['engine_config'].keys() == {'containers_conf', 'storage_conf'} and all(_hex(v) for v in doc['engine_config'].values()) and
                 type(doc['inspector']) is dict and set(doc['inspector']) == {'closure_sha256', 'positive', 'negative'} and
                 _hex(doc['inspector']['closure_sha256']) and run(doc['inspector']['positive']) and run(doc['inspector']['negative']) and
                 type(doc['containment']) is dict and set(doc['containment']) == set(PROBES) and
                 all(doc['containment'][name] == {'status': 'pass'} for name in PROBES))
    except (AttributeError, KeyError, TypeError):
        valid = False
    require(valid, 'native_qualification_incomplete')
    return doc


def match_qualification(doc, pending, input_value, builds):
    """Measured records against the independently admitted closure/engine/confs/host/lanes."""
    qualification_complete(doc)
    capability = pending.capability
    descriptor = thaw(capability.descriptor)['descriptor']
    host = thaw(pending.host)
    require(doc['architecture'] == input_value['builder_requirements']['architecture'] and
            doc['platform'] == input_value['target']['oci_platform'] and
            doc['engine'] == input_value['builder_requirements']['engine'] and
            all(builds[slot]['observed_builder']['engine'] == doc['engine'] for slot in ('A', 'B')) and
            doc['engine_path'] == str(capability.engine) and doc['engine']['sha256'] == capability.engine_sha256 and
            doc['inspector']['closure_sha256'] == capability.closure_sha256 and
            doc['engine_config'] == {name: descriptor['engine_config'][name]['sha256'] for name in ('containers_conf', 'storage_conf')} and
            all(doc['host'][key] == host.get(key) for key in ('os', 'arch', 'uid', 'rootless')),
            'native_inspector_execution_unqualified')
    return MatchedQualification(capability.closure_sha256, doc['architecture'], _SEAL)


def _native_children(build, measured, inventory, doc):
    """Bind every phase's ordered executed children; the build child is exactly
    owned prefix + admitted template in the bound clone cwd (Hale R4-MAP-001)."""
    rows = {row['path']: row for row in inventory}
    found = {}
    observed = build['observed_builder']
    for op in build['operations']:
        path = op['evidence']['path']
        require(path.endswith('.json'), 'native_child_evidence')
        path = path[:-5] + '.children.json'
        require(path in rows and path in measured, 'native_child_evidence')
        children = decode_json(measured[path], 8 * 1024 * 1024)
        require(type(children) is list and all(
            type(child) is dict and child.get('exit') == 0 and child.get('environment_names') == doc['engine_environment']
            for child in children), 'native_child_evidence')
        if op['phase'] == 'build':
            argv = children[0].get('argv') if len(children) == 1 else None
            require(type(argv) is list and argv[:6] == [doc['engine_path'], '--remote=false', '--root', observed['store_id'],
                                                        '--runroot', observed['runroot_id']] and
                    argv[6:7] == ['--tmpdir'] and argv[8:] == op['argv'] and children[0].get('cwd') == observed['source_id'],
                    'native_child_evidence')
        found[path] = rows[path]
    return found


def parse_build_receipt(raw_bytes):
    value = decode_json(raw_bytes, 8 * 1024 * 1024)
    schema_validate(value, 'build-receipt.schema.json')
    ordered_inventory(value['evidence']['inventory'])
    for build in value['builds'].values():
        ordered_inventory(build['outputs'])
    return ParsedBuildReceipt(raw_bytes, sha256(raw_bytes), freeze(value))


def _json_record(record, measured):
    require(record['media_type'] == 'application/json', 'evidence_media_type')
    require(record['path'] in measured, 'unindexed_evidence')
    return decode_json(measured[record['path']], 8 * 1024 * 1024)


def verify_build_receipt(receipt, verified_input, output_reader, evidence_reader, qualification_policy):
    require(isinstance(verified_input, VerifiedBuildInput) and verified_input._seal is _SEAL, 'unverified_input')
    require(isinstance(receipt, ParsedBuildReceipt), 'unparsed_receipt')
    policy = qualification_policy
    require(isinstance(policy, dict), 'invalid_policy_shape')
    scope = thaw(verified_input.document)['admission_policy']['qualification_scope']
    if scope == 'native':
        # Acceptance of exact original native bytes/policy precedes reparse,
        # readers, OCI/marshal inspection and any compiler authority.
        require(type(receipt.raw) is bytes and len(receipt.raw) <= 8*1024*1024 and
                policy.get('receipt_sha256') == sha256(receipt.raw), 'native_receipt_not_independently_accepted')
        require(policy.get('policy_sha256') == thaw(verified_input.document)['admission_policy']['policy_sha256'],
                'receipt_audit_policy')
        # Vera Q2 order, step 1: closure/ABI/host preflight only; no session yet.
        from .inspector import preflight
        pending = preflight(verified_input, policy)
    else:
        pending = None
    inspection = None
    parsed = parse_build_receipt(receipt.raw)
    require(parsed.sha256 == receipt.sha256 and parsed.document == receipt.document, 'changed_parsed_receipt')
    value, input_value = thaw(parsed.document), thaw(verified_input.document)
    policy = qualification_policy
    scope = input_value['admission_policy']['qualification_scope']
    require(value['input_sha256'] == verified_input.input_sha256 == policy.get('input_sha256') and
            value['target'] == thaw(verified_input.target), 'receipt_input_binding')
    require(value['qualification_scope'] == scope == policy.get('qualification_scope') and
            (scope != 'native' or verified_input.native_qualified), 'receipt_qualification_scope')
    require(value['result'] == 'PASS' and value['reason'] == 'complete', 'receipt_not_pass')
    audit = value['audit']
    require(audit['policy_sha256'] == policy.get('policy_sha256') == input_value['admission_policy']['policy_sha256'] and
            audit['policy_id'] == input_value['admission_policy']['policy_id'], 'receipt_audit_policy')
    require(audit['required_classes'] == list(CLASSES) == policy.get('required_classes') and not audit['findings'], 'audit_class_closure')
    limit = input_value['resources']['disk_bytes']
    inventory = value['evidence']['inventory']
    index_record = value['evidence']['index']
    require(index_record['role'] == 'evidence-index' and index_record['path'] not in [r['path'] for r in inventory], 'evidence_index_cycle')
    measured = {r['path']: measure(r, evidence_reader, min(limit, 8 * 1024 * 1024)) for r in inventory + [index_record]}
    require(_json_record(index_record, measured) == {'inventory': inventory}, 'evidence_index_closure')
    required_evidence, output_paths, builds = {}, set(), value['builds']
    if scope == 'native':
        # Steps 2-4: measured closed native roles, matched to the accepted context,
        # then the ONE session. Exact-set evidence closure stays below.
        rows = [row for row in inventory if row['role'] == 'native.qualification']
        require(len(rows) == 1, 'native_qualification_incomplete')
        record = rows[0]
        doc = decode_json(measured[record['path']], 1024 * 1024)
        matched = match_qualification(doc, pending, input_value, builds)
        for slot in ('A', 'B'):
            required_evidence.update(_native_children(builds[slot], measured, inventory, doc))
        required_evidence[record['path']] = record
        from .inspector import admit_session
        inspection = admit_session(pending, matched)
    for slot in ('A', 'B'):
        observed = builds[slot]['observed_builder']
        require(observed['build_id'] == builds[slot]['build_id'], 'observed_build_binding')
        require(observed['qualification_scope'] == scope and observed['os'] == input_value['builder_requirements']['os'] and
                observed['architecture'] == input_value['builder_requirements']['architecture'] and
                observed['rootless'] and observed['uid'] >= input_value['builder_requirements']['uid_min'] and
                observed['toolchain'] == input_value['builder_requirements']['toolchain'] and
                observed['engine'] == input_value['builder_requirements']['engine'] and
                observed['fresh_store'] and not observed['mutable_shared'], 'observed_builder_requirements')
    require(all(builds['A']['observed_builder'][key] != builds['B']['observed_builder'][key] for key in IDENTITIES), 'independent_builds')
    archives, closures, semantic = [], [], []
    all_outputs = []
    for slot in ('A', 'B'):
        build = builds[slot]
        require(build['source_sha256'] == sha256(canonical_bytes(input_value['source'])) and
                build['recipe_sha256'] == sha256(canonical_bytes(input_value['recipe'])) and
                build['dependency_sha256'] == sha256(canonical_bytes(input_value['dependencies'])), 'build_input_components')
        start, finish = build['started_at_unix_ns'], build['finished_at_unix_ns']
        require(start <= finish and finish - start <= input_value['resources']['absolute_timeout_sec'] * 10**9, 'build_time_budget')
        operations = build['operations']
        require([op['phase'] for op in operations] == list(PHASES), 'executed_phase_closure')
        previous = start
        for op in operations:
            require(op['status'] == 'exited' and op['exit_code'] == 0 and
                    previous <= op['started_at_unix_ns'] <= op['finished_at_unix_ns'] <= finish and
                    op['finished_at_unix_ns'] - op['started_at_unix_ns'] <= input_value['resources']['phase_timeouts_sec'][op['phase']] * 10**9,
                    'operation_exit_or_budget')
            require(op['environment_names'] == input_value['determinism']['environment']['names'] and
                    op['environment_policy_sha256'] == audit['policy_sha256'], 'operation_environment_policy')
            if op['phase'] == 'build':
                require(op['argv'] == input_value['recipe']['argv_template'], 'operation_recipe_argv')
            record = op['evidence']
            expected = {k: v for k, v in op.items() if k != 'evidence'}
            expected.update(build_id=build['build_id'], qualification_scope=scope)
            require(_json_record(record, measured) == expected, 'operation_raw_evidence')
            required_evidence[record['path']] = record
            previous = op['finished_at_unix_ns']
        outputs = build['outputs']
        require(len({r['role'] for r in outputs}) == len(outputs) and
                not output_paths.intersection(r['path'] for r in outputs), 'output_role_or_path_alias')
        output_paths.update(r['path'] for r in outputs)
        actual = {r['path']: measure(r, output_reader, limit) for r in outputs}
        roles = {r['role']: r for r in outputs}
        require('archive' in roles and roles['archive']['media_type'] == 'application/x-tar', 'raw_archive_media')
        archive = actual[roles['archive']['path']]
        closure, layer_inventory = inspect_oci(archive, outputs, actual, build['oci'], input_value,
                                              module_policy=thaw(verified_input.metadata)['release_policy'],
                                              accepted_inputs=thaw(verified_input.metadata)['accepted_inputs'],
                                              inspection=inspection)
        require(build['provenance'] == roles['provenance'] and roles['provenance']['media_type'] == 'application/json', 'provenance_record_binding')
        provenance = decode_json(actual[roles['provenance']['path']], 8 * 1024 * 1024)
        expected = {key: build[key] for key in ('build_id','source_sha256','recipe_sha256','dependency_sha256','observed_builder')}
        expected.update(input_sha256=verified_input.input_sha256, output_sha256=sha256(archive),
                        policy_sha256=audit['policy_sha256'], qualification_scope=scope)
        require(provenance == expected, 'provenance_binding')
        require(roles['sbom']['media_type'] == 'application/json' and
                decode_json(actual[roles['sbom']['path']], 8 * 1024 * 1024) ==
                {'output_sha256': sha256(archive), 'inventory_sha256': sha256(canonical_bytes(layer_inventory))}, 'sbom_binding')
        require(set(audit['reports'][slot]) == set(CLASSES), 'semantic_report_closure')
        semantic_rows = []
        for category in CLASSES:
            record = audit['reports'][slot][category]
            report = _json_record(record, measured)
            require(report == {'build_id': build['build_id'], 'class': category, 'output_sha256': sha256(archive),
                               'inventory_sha256': sha256(canonical_bytes(layer_inventory)), 'findings': []}, 'semantic_report_binding')
            semantic_rows.append({k:v for k,v in report.items() if k != 'build_id'})
            required_evidence[record['path']] = record
        custody = build['custody']
        require(_json_record(custody, measured) == {'build_id': build['build_id'], 'outputs': outputs, 'complete': True}, 'build_custody')
        cleanup = build['cleanup']
        resources = [build['observed_builder'][key] for key in IDENTITIES if key != 'build_id']
        require(_json_record(cleanup, measured) == {'build_id': build['build_id'], 'removed_resources': resources,
                'remaining_owned_resources': [], 'complete': True}, 'build_cleanup')
        required_evidence[custody['path']] = custody
        required_evidence[cleanup['path']] = cleanup
        archives.append(archive)
        closures.append(closure)
        semantic.append(semantic_rows)
        all_outputs.extend(outputs)
    comparison = value['comparison']
    require(archives[0] == archives[1] and comparison == {'raw_archives_equal': True,
            'raw_archive_sha256': sha256(archives[0]), 'raw_archive_bytes': len(archives[0]),
            'oci_closure_equal': True, 'semantic_audit_equal': True} and closures[0] == closures[1] and
            semantic[0] == semantic[1], 'raw_equality_and_audit')
    custody, cleanup = value['custody'], value['cleanup']
    require(custody['complete'] and not custody['partial'] and not custody['missing_roles'] and
            _json_record(custody['receipt'], measured) == {'complete': True, 'partial': False, 'missing_roles': [],
            'outputs': sorted(all_outputs, key=lambda r:r['path'])}, 'complete_custody')
    require(cleanup['complete'] and not cleanup['remaining_owned_resources'] and
            _json_record(cleanup['receipt'], measured) == {'build_ids': [builds[s]['build_id'] for s in ('A','B')],
            'complete': True, 'remaining_owned_resources': []}, 'complete_cleanup')
    required_evidence[custody['receipt']['path']] = custody['receipt']
    required_evidence[cleanup['receipt']['path']] = cleanup['receipt']
    require(sorted(required_evidence.values(), key=lambda r:r['path']) == inventory, 'indexed_evidence_complete')
    if scope == 'native':
        require(policy.get('receipt_sha256') == parsed.sha256, 'native_receipt_not_independently_accepted')
    return VerifiedBuildReceipt(parsed.sha256, verified_input.input_sha256, freeze(value['target']),
                                sha256(archives[0]), freeze({p:sha256(b) for p,b in measured.items()}),
                                scope == 'native', freeze(value), _SEAL)


def verify_native_receipt_bytes(raw, verified_input, output_reader, evidence_reader, qualification_policy):
    """Bound exact original native receipt before even JSON/schema decoding."""
    require(isinstance(verified_input, VerifiedBuildInput) and verified_input._seal is _SEAL, 'unverified_input')
    require(isinstance(qualification_policy, dict) and type(raw) is bytes and len(raw)<=8*1024*1024 and
            qualification_policy.get('receipt_sha256')==sha256(raw), 'native_receipt_not_independently_accepted')
    require(qualification_policy.get('policy_sha256')==thaw(verified_input.document)['admission_policy']['policy_sha256'], 'receipt_audit_policy')
    # Vera Q2 step 1 before any decoding: closure/ABI/host eligibility only. No session
    # here; verify_build_receipt runs the one preflight/match/session order.
    from .inspector import preflight
    preflight(verified_input, qualification_policy)
    return verify_build_receipt(parse_build_receipt(raw), verified_input, output_reader, evidence_reader, qualification_policy)
