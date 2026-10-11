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
    from cortex_core.release.receipts import parse_build_receipt, verify_build_receipt

    receipt = parse_build_receipt(raw)
    return verify_build_receipt(
        receipt, verified_input, output_reader, evidence_reader, qualification_policy
    )
