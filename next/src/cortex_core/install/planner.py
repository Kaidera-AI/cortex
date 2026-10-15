"""Read-only source planner. No process/host/DB/credential/effect ports exist.

The sole signed-member seam is RC2; its production UNBOUND precedes request,
package or config reads. Any source success remains non-native/non-authoritative.
"""
from dataclasses import dataclass

from cortex_core.release.codec import canonical_bytes, decode_json, sha256
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import signed_member
from cortex_core.runtime.config import RUNTIME_LOCK_MEMBER, admit_config
from cortex_core.runtime.contracts import MAX_CONFIG_BYTES, decode

from .contracts import InstallRefusal, absolute_path, measure_original_ref, parse_install_message
from .receipt import readonly_receipt


@dataclass(frozen=True)
class PlanResult:
    result: str
    reason_code: str
    unit: str | None = None
    receipt: object = None
    runtime_plan: object = None
    evidence_records: object = None
    authority_status: str = 'UNBOUND'
    effect_authority: bool = False
    native_qualified: bool = False


def dependency(unit):
    return PlanResult('BLOCKED', 'dependency_not_run', unit)


def _require(condition, reason):
    if not condition:
        raise InstallRefusal(reason)


def _missing(value):
    if isinstance(value, dict):
        if value.get('state') in ('GAP', 'UNBOUND'):
            return True
        return any(_missing(child) for child in value.values())
    if isinstance(value, list):
        return any(_missing(child) for child in value)
    return False


def _envelopes(raws):
    if raws is None:
        return
    _require(type(raws) is dict and set(raws) == {'core', 'api'}, 'bad_shape')
    try:
        values = [decode_json(raws[name], 1048576) for name in ('core', 'api')]
    except (ValueError, TypeError):
        raise InstallRefusal('bad_shape') from None
    if any(_missing(value) for value in values):
        raise InstallRefusal('dependency_not_run', unit='Core/API recipe source envelopes')
    # Reuse Ren-KOS's source grammar. It supplies no native/owner capability.
    from cortex_core.release.build.envelopes import SourceRefusal, parse_source_envelope
    try:
        for raw in raws.values():
            parse_source_envelope(raw)
    except SourceRefusal:
        raise InstallRefusal('bad_shape') from None


def _original(record, *, aggregate_id, signing_root_id):
    member = signed_member.request_member(aggregate_id, record['path'])
    raw = signed_member.read_member(member, aggregate_id=aggregate_id, member_name=record['path'])
    _require(member.signing_root_id == signing_root_id, 'wrong_release')
    return measure_original_ref(record, raw)


def _roots(q, *, aggregate_id, signing_root_id, owner_uid):
    declared_identities = []
    for root in q['roots']:
        _require(root['owner_uid'] == owner_uid, 'unsafe_custody')
        raw = _original(root['custody_ref'], aggregate_id=aggregate_id, signing_root_id=signing_root_id)
        try:
            witness = decode_json(raw, 1048576)
        except ValueError:
            raise InstallRefusal('unsafe_custody') from None
        # These are original declaration witnesses, not physical custody proof.
        _require(type(witness) is dict and set(witness) == {'kind', 'canonical_path', 'backend_id', 'filesystem_identity',
                 'uid', 'mode', 'content_ref', 'executable_ref', 'link_target', 'backend_witness_ref'}, 'unsafe_custody')
        _require(witness['kind'] == 'directory' and witness['canonical_path'] == root['absolute_path']
                 and type(witness['uid']) is int and witness['uid'] == owner_uid and witness['mode'] == 0o700
                 and witness['link_target'] is None and type(witness['filesystem_identity']) is dict
                 and set(witness['filesystem_identity']) == {'device', 'inode'}
                 and all(type(v) is int and v >= 0 for v in witness['filesystem_identity'].values()), 'unsafe_custody')
        identity = witness['filesystem_identity']
        if identity['device'] > 0 and identity['inode'] > 0:
            key = (witness['backend_id'], identity['device'], identity['inode'])
            _require(key not in declared_identities, 'unsafe_custody')
            declared_identities.append(key)


def plan(original_request, *, aggregate_id, observed_target, owner_uid, host_identity_ref,
         environ, config_reader, source_envelopes=None):
    """Produce a source-only plan or typed missing-input result, never effects.

    config_reader is RC2's existing owner-composed config reader. It is reached
    only after the original signed member and source preconditions. No default
    account, target, input path, runtime or ambient environment is discovered.
    """
    try:
        _envelopes(source_envelopes)
        member = signed_member.request_member(aggregate_id, RUNTIME_LOCK_MEMBER)
        lock_raw = signed_member.read_member(member, aggregate_id=aggregate_id, member_name=RUNTIME_LOCK_MEMBER)
        lock = decode(lock_raw, limit=262144)
        if _missing(lock):
            return dependency('runtime lock owner bindings')
        parsed = parse_install_message(original_request, expected_kind='request')
        q = thaw(parsed.document)
        if q['operation'] not in ('plan', 'verify'):
            return dependency('P2 installer lifecycle/status/recovery')
        if q['identity']['product'] != 'cortex':
            return dependency('product owner interface')
        if q['acquisition']['mode'] != 'offline':
            return dependency('accepted acquisition/quarantine policy')
        _require(type(owner_uid) is int and owner_uid == q['caller']['owner_uid']
                 and host_identity_ref == q['caller']['host_identity_ref'], 'unsafe_custody')
        # No key/value from an ambient environment is implicitly admitted.
        _require(type(environ) is dict and not environ, 'unsafe_custody')
        expected = {'linux/arm64': ('darwin', 'arm64'), 'linux/amd64': ('linux', 'x86_64')}
        _require(observed_target in expected and q['target']['backend_platform'] == observed_target
                 and (q['target']['host_os'], q['target']['host_arch']) == expected[observed_target], 'unsupported_target')
        if q['release']['qualification_scope'] != 'fixture':
            return dependency('native material and physical owner/custody qualification')
        _require(q['invocation']['entrypoint_ref'] == q['release']['installer_ref']
                 and q['target']['compatibility_ref'] == q['release']['compatibility_ref'], 'wrong_release')
        argv = q['invocation']['argv']
        _require(len(argv) == 4 and argv[0] == q['invocation']['executable_path']
                 and argv[1] == '--request-file' and argv[3] == '--json', 'bad_shape')
        absolute_path(argv[2])
        _require(q['invocation']['cwd_root'] in [r['absolute_path'] for r in q['roots']], 'unsafe_custody')
        try:
            config_raw = config_reader('/run/cortex/inspection.json', MAX_CONFIG_BYTES)
        except Exception:
            return dependency('runtime inspection config')
        runtime = admit_config(member, config_raw, aggregate_id=aggregate_id, observed_target=observed_target, mode='inspect')
        cfg = decode(config_raw, limit=MAX_CONFIG_BYTES)
        _require(q['identity']['operation_id'] == cfg['operation_id']
                 and (q['identity']['installation_id'] is None or q['identity']['installation_id'] == cfg['installation_id']), 'state_drift')
        records = []
        for key, value in q['release'].items():
            if key.endswith('_ref'):
                records.append(value)
        records.extend(q['release']['artifact_refs'])
        records.extend([q['caller']['host_identity_ref'], q['invocation']['environment_policy_ref']])
        # Retain each exact reference; distinct logical members cannot collide.
        selected = {}
        for record in records:
            if record['path'] in selected:
                _require(selected[record['path']] == record, 'wrong_release')
            selected[record['path']] = record
            _original(record, aggregate_id=aggregate_id, signing_root_id=member.signing_root_id)
        _roots(q, aggregate_id=aggregate_id, signing_root_id=member.signing_root_id, owner_uid=owner_uid)
        for root in q['roots']:
            record = root['custody_ref']
            _require(record['path'] not in selected or selected[record['path']] == record, 'wrong_release')
            selected[record['path']] = record
        for record in selected.values():
            _original(record, aggregate_id=aggregate_id, signing_root_id=member.signing_root_id)
        # Source producer records are immutable in-memory outputs for the caller
        # to retain in its evidence sink. No journal/product/cache path is written.
        ledger = canonical_bytes({'scope': 'fixture-read-only', 'forbidden_attempts': []})
        attempts_ref = {'path': 'attempts.json', 'bytes': len(ledger), 'sha256': sha256(ledger)}
        request_ref = {'path': 'request.json', 'bytes': len(original_request), 'sha256': sha256(original_request)}
        prior = [q[k] for k in ('previous_receipt_ref', 'journal_ref') if q[k] is not None]
        if prior:
            # Live previous ownership requires P2/owner readers; never invent it
            # from a name, caller flag or merely parsed previous receipt.
            return dependency('prior installation live ownership/receipt/journal inspection')
        projected = readonly_receipt(parsed, request_ref=request_ref, attempts_ref=attempts_ref,
                                     evidence_refs=list(selected.values()))
        return PlanResult('PASS', 'completed', receipt=projected, runtime_plan=runtime,
                          evidence_records=freeze({'request.json': original_request, 'attempts.json': ledger}))
    except signed_member.Refusal as refusal:
        if refusal.reason == 'required_binding_gap':
            return dependency(signed_member.MISSING_UNIT if refusal.unit == signed_member.MISSING_UNIT else 'runtime ABI owner bindings')
        return PlanResult('FAIL', 'wrong_release')
    except InstallRefusal as refusal:
        return PlanResult('BLOCKED' if refusal.reason_code in ('dependency_not_run', 'missing_contract') else 'FAIL',
                          refusal.reason_code, refusal.unit)
