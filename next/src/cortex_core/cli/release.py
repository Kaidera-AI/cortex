"""Public build admission CLI. Decisions are data, never effect authority."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import re
from cortex_core.release.codec import canonical_bytes, decode_json, sha256
from cortex_core.release.custody import RootReader
from cortex_core.release.inputs import admit_build_input, parse_build_input
from cortex_core.release.receipts import parse_build_receipt, verify_build_receipt


def _document(path, digest, limit):
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('invalid_external_digest')
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or sha256(raw) != digest:
        raise ValueError('external_record_mismatch')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('admit-build', 'verify-build-receipt'):
        command = commands.add_parser(name)
        for flag in ('input','input-sha256','input-root','policy','policy-record-sha256','decision'):
            command.add_argument('--'+flag, required=True)
        if name == 'verify-build-receipt':
            for flag in ('receipt','receipt-sha256','output-root','evidence-root'):
                command.add_argument('--'+flag, required=True)
    args = parser.parse_args(argv)
    try:
        policy = decode_json(_document(args.policy, args.policy_record_sha256, 1024*1024), 1024*1024)
        if not isinstance(policy, dict):
            raise ValueError('invalid_policy_shape')
        parsed = parse_build_input(_document(args.input, args.input_sha256, 1024*1024))
        limit = parsed['resources']['disk_bytes']
        with ExitStack() as resources:
            reader = RootReader(args.input_root, limit)
            resources.callback(reader.close)
            admitted = admit_build_input(parsed, reader, policy)
            result = {'decision': 'admitted', 'input_sha256': admitted.input_sha256,
                      'native_qualified': admitted.native_qualified}
            if args.command == 'verify-build-receipt':
                raw_receipt = _document(args.receipt, args.receipt_sha256, 8*1024*1024)
                if parsed['admission_policy']['qualification_scope'] == 'native':
                    from cortex_core.release.codec import sha256
                    if policy.get('receipt_sha256') != sha256(raw_receipt):
                        raise ValueError('native_receipt_not_independently_accepted')
                    if policy.get('policy_sha256') != parsed['admission_policy']['policy_sha256']:
                        raise ValueError('receipt_audit_policy')
                    from cortex_core.release.inspector import admit_inspection
                    admit_inspection(admitted, policy)
                receipt = parse_build_receipt(raw_receipt)
                outputs = RootReader(args.output_root, limit)
                resources.callback(outputs.close)
                evidence = RootReader(args.evidence_root, min(limit,8*1024*1024))
                resources.callback(evidence.close)
                verified = verify_build_receipt(receipt, admitted, outputs.bytes, evidence.bytes, policy)
                result = {'decision': 'verified', 'receipt_sha256': verified.receipt_sha256,
                          'input_sha256': verified.input_sha256, 'output_sha256': verified.output_sha256,
                          'native_qualified': verified.native_qualified}
        code = 0
    except (ValueError, OSError) as error:
        # Bound reasons, never reflect untrusted paths/values or errno messages.
        reason = str(error) if isinstance(error, ValueError) else 'read_unavailable'
        if not re.fullmatch('[a-z_]+(?::[A-Za-z0-9_/]+)?', reason):
            reason = 'invalid_contract'
        result, code = {'decision': 'refused', 'reason': reason}, 2
    Path(args.decision).write_bytes(canonical_bytes(result))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
