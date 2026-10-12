"""R02 receipt plumbing; R01 owns every wire parser and policy."""

import hashlib


def require_complete(receipt):
    if (
        receipt.get("result") != "PASS"
        or type(receipt.get("exit")) is not int
        or receipt["exit"] != 0
        or receipt.get("custody") is not True
        or receipt.get("cleanup") is not True
    ):
        raise ValueError("actual operation/custody/cleanup incomplete")
    return True


def verify_checked_bytes(raw, expected):
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("checked bytes differ; no execution allowed")
    return raw


def verify_shared_receipt(
    raw, verified_input, output_reader, evidence_reader, qualification_policy
):
    from cortex_core.release.receipts import (
        parse_build_receipt,
        verify_build_receipt,
        verify_native_receipt_bytes,
    )
    from .inputs import require_verified

    require_verified(verified_input)
    if verified_input.document["admission_policy"]["qualification_scope"] == "native":
        # The owner admits original bytes, policy and closure before parsing.
        return verify_native_receipt_bytes(
            raw, verified_input, output_reader, evidence_reader, qualification_policy
        )
    receipt = parse_build_receipt(raw)
    return verify_build_receipt(
        receipt, verified_input, output_reader, evidence_reader, qualification_policy
    )


def emit(
    verified_input,
    builds,
    audit,
    evidence,
    custody,
    cleanup,
    output_reader,
    evidence_reader,
    qualification_policy,
):
    """Emit only the owner schema and verify original bytes before returning it.

    The caller supplies measured build/audit records. R01 remeasures every output,
    evidence link and required class. This function grants no build or deploy effect.
    """
    from cortex_core.release.codec import canonical_bytes
    from cortex_core.release.contracts import thaw
    from cortex_core.release.artifacts import measure
    from .inputs import require_verified

    require_verified(verified_input)
    if set(builds) != {"A", "B"}:
        raise ValueError("exact independent A/B records required")
    archives = []
    for slot in ("A", "B"):
        candidates = [
            row for row in builds[slot]["outputs"] if row["role"] == "archive"
        ]
        if len(candidates) != 1:
            raise ValueError("one actual raw archive per build required")
        archives.append(
            measure(
                candidates[0],
                output_reader,
                verified_input.document["resources"]["disk_bytes"],
            )
        )
    if archives[0] != archives[1]:
        raise ValueError("whole independent archive bytes differ")
    raw_archive = archives[0]
    value = {
        "schema": "cortex.build-receipt.v1",
        "format_version": 1,
        "input_sha256": verified_input.input_sha256,
        "target": thaw(verified_input.target),
        "qualification_scope": verified_input.document["admission_policy"][
            "qualification_scope"
        ],
        "result": "PASS",
        "reason": "complete",
        "builds": builds,
        "comparison": {
            "raw_archives_equal": True,
            "raw_archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
            "raw_archive_bytes": len(raw_archive),
            "oci_closure_equal": True,
            "semantic_audit_equal": True,
        },
        "audit": audit,
        "evidence": evidence,
        "custody": custody,
        "cleanup": cleanup,
    }
    raw = canonical_bytes(value)
    verified = verify_shared_receipt(
        raw, verified_input, output_reader, evidence_reader, qualification_policy
    )
    return raw, verified
