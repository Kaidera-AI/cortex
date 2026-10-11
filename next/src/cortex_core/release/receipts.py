"""Shared build receipt verifier; no assembly, signing or installation effects."""
from .artifacts import inspect_oci, measure, require
from .codec import canonical_bytes, decode_json, ordered_inventory, schema_validate, sha256
from .contracts import ParsedBuildReceipt, ReceiptRefusal, VerifiedBuildInput, VerifiedBuildReceipt, _SEAL, freeze, thaw

CLASSES = ('accounts', 'cache', 'clock', 'files', 'input', 'labels', 'logs', 'python', 'runtime', 'serialization', 'xattrs-acls')
PHASES = ('input', 'parser', 'build', 'export', 'audit', 'cleanup')
IDENTITIES = ('build_id', 'execution_id', 'store_id', 'runroot_id', 'container_id', 'source_id')


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
        closure, layer_inventory = inspect_oci(archive, outputs, actual, build['oci'], input_value)
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
