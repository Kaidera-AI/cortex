"""Strict pre-build parser and admission over independently accepted bytes."""
from dataclasses import dataclass
from .codec import canonical_bytes, decode_json, ordered_inventory, relative_path, schema_validate, sha256
from .contracts import BuildRefusal, ParsedBuildInput, VerifiedBuildInput, _SEAL, freeze, thaw


@dataclass(frozen=True)
class FileObservation:
    data: bytes
    kind: str
    mode: int
    link_target: str | None = None


def parse_build_input(raw_bytes):
    value = decode_json(raw_bytes, 1024 * 1024)
    schema_validate(value, 'build-input.schema.json')
    for item in (value['source']['inventory'], value['recipe']['inventory'],
                 value['dependencies']['locks'], value['dependencies']['assets'],
                 value['compatibility_inputs']['event_schemas'],
                 value['compatibility_inputs']['dependency_interfaces']):
        ordered_inventory(item)
    relative_path(value['source']['context_root'])
    relative_path(value['recipe']['entrypoint'])
    for arg in value['recipe']['argv_template']:
        if len(arg.encode('utf-8')) > 4096:
            raise BuildRefusal('argv_bound')
    return ParsedBuildInput(raw_bytes, sha256(raw_bytes), freeze(value))


def input_records(value):
    compatibility = value['compatibility_inputs']
    rows = list(value['source']['inventory']) + [value['descriptor']] + list(value['recipe']['inventory'])
    rows += list(value['dependencies']['locks']) + list(value['dependencies']['assets'])
    rows += [image['provenance'] for image in value['dependencies']['base_images']]
    rows += [compatibility[key] for key in ('compatibility_contract', 'schema_manifest', 'api_descriptor', 'event_index')]
    rows += list(compatibility['event_schemas']) + list(compatibility['dependency_interfaces'])
    seen = {}
    for row in rows:
        relative_path(row['path'])
        if row['path'] in seen and row != seen[row['path']]:
            raise BuildRefusal('conflicting_input_member')
        seen[row['path']] = row
    return [seen[path] for path in sorted(seen)]


def admit_build_input(input, input_reader, accepted_policy):
    if not isinstance(accepted_policy, dict):
        raise BuildRefusal('invalid_policy_shape')
    if not isinstance(input, ParsedBuildInput):
        raise BuildRefusal('unparsed_input')
    # Reparse rather than accepting a hand-constructed ParsedDocument capability.
    parsed = parse_build_input(input.raw)
    if parsed.sha256 != input.sha256 or parsed.document != input.document:
        raise BuildRefusal('changed_parsed_input')
    value = thaw(parsed.document)
    policy = value['admission_policy']
    if (accepted_policy.get('input_sha256') != parsed.sha256 or
        accepted_policy.get('policy_sha256') != policy['policy_sha256'] or
        accepted_policy.get('qualification_scope') != policy['qualification_scope']):
        raise BuildRefusal('unaccepted_input_or_policy')
    native = policy['qualification_scope'] == 'native'
    requirements = value['builder_requirements']
    if requirements['architecture'] != value['target']['oci_platform'].split('/')[1]:
        raise BuildRefusal('platform_requirements_mismatch')
    if value['determinism']['policy_id'] != policy['policy_id']:
        raise BuildRefusal('determinism_policy_mismatch')
    python = value['determinism']['python']
    if python['applicable'] and python['compiler_sha256'] != requirements['toolchain']['sha256']:
        raise BuildRefusal('compiler_policy_mismatch')
    phases = value['resources']['phase_timeouts_sec']
    if max(phases.values()) > value['resources']['absolute_timeout_sec']:
        raise BuildRefusal('phase_budget_exceeds_absolute')
    source_paths = [r['path'] for r in value['source']['inventory']]
    if any(not p.startswith(value['source']['context_root'] + '/') for p in source_paths):
        raise BuildRefusal('source_outside_context')
    if value['recipe']['entrypoint'] not in [r['path'] for r in value['recipe']['inventory']]:
        raise BuildRefusal('recipe_entrypoint_missing')
    argv = value['recipe']['argv_template']
    if value['recipe']['entrypoint'] not in argv or (argv[0].split('/')[-1] in ('sh','bash','dash','zsh','ksh') and
            any(arg.startswith('-') and 'c' in arg[1:] for arg in argv[1:])):
        raise BuildRefusal('recipe_not_literal_entrypoint')
    for image in value['dependencies']['base_images']:
        if image['platform'] != value['target']['oci_platform'] or not image['reference'].endswith('@sha256:' + image['manifest_digest']):
            raise BuildRefusal('unpinned_base_image')
    if native:
        inventory = getattr(input_reader, 'inventory', None)
        if not callable(inventory) or sorted(inventory(value['source']['context_root'])) != source_paths:
            raise BuildRefusal('unmeasured_complete_inventory')
    measured = []
    actual = {}
    for row in input_records(value):
        try:
            observed = input_reader(row['path'])
        except (OSError, KeyError, ValueError) as error:
            raise BuildRefusal('input_unavailable') from error
        if isinstance(observed, FileObservation):
            raw = observed.data
            if observed.kind != row['kind'] or observed.mode != row['mode'] or observed.link_target != row.get('link_target'):
                raise BuildRefusal('input_metadata_mismatch')
        elif type(observed) is bytes and not native and row['kind'] == 'regular':
            raw = observed
        else:
            raise BuildRefusal('input_metadata_unavailable')
        if row['kind'] == 'symlink':
            if row['path'] not in accepted_policy.get('allowed_symlinks', {}) or accepted_policy['allowed_symlinks'][row['path']] != row['link_target']:
                raise BuildRefusal('symlink_not_accepted')
            relative_path(row['link_target'])
        if type(raw) is not bytes or len(raw) != row['bytes'] or sha256(raw) != row['sha256']:
            raise BuildRefusal('input_bytes_mismatch')
        actual[row['path']] = raw
        measured.append(row)
    descriptor = decode_json(actual[value['descriptor']['path']], 1024 * 1024, canonical=False)
    if descriptor.get('module_id') != value['target']['module_id'] or descriptor.get('module_version') != value['target']['module_version']:
        raise BuildRefusal('descriptor_target_mismatch')
    return VerifiedBuildInput(parsed.sha256, freeze(value['target']), tuple(freeze(r) for r in measured),
                              freeze(requirements), freeze(value), native, _SEAL)
