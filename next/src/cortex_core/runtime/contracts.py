"""Pinned RC0 wire contract; reuse R01 decoding, never a second declaration ABI."""
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from cortex_core.release.codec import decode_json, sha256
from .signed_member import Refusal

MAX_LOCK_BYTES = 262144
MAX_CONFIG_BYTES = 65536
RC0_SHA256 = {
    'runtime-lock-v1.schema.json': 'f7b81910c0bb0de651417054bf46c591916175118ca9eb5fbb849310e5bcf008',
    'runtime-config-v1.schema.json': 'c0f05c8f8ad442dfe781bee08a0eee8926b0d9331e33790c0f2f87c069b78efb',
    'runtime-operations-v1.json': '32eb25a99a6fdf94f770f5d6b1584ff4b58801686f96a57ad6259788ec8b94a0',
}


def decode(raw: bytes, *, limit: int):
    if type(raw) is not bytes or len(raw) > limit:
        raise Refusal('raw_limit')
    try:
        value = decode_json(raw, limit, canonical=False)
    except (ValueError, RecursionError, OverflowError):
        raise Refusal('raw_shape') from None

    def depth(item, level):
        if level > 32:
            raise Refusal('raw_limit')
        if isinstance(item, dict):
            for key, child in item.items():
                depth(key, level + 1)
                depth(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                depth(child, level + 1)
    depth(value, 0)
    return value


@dataclass(frozen=True)
class RuntimeContracts:
    lock_validator: Draft202012Validator
    config_validator: Draft202012Validator
    operations: dict
    operations_sha256: str


def load_contracts(*, operations_raw: bytes | None = None) -> RuntimeContracts:
    """Read only this package's fixed RC0 members, after signed-member admission.

    The optional operation bytes support an explicit transport, not policy:
    byte-for-byte pinning applies equally to both sources. No schema URL fetches.
    """
    root = Path(__file__).resolve().parents[3] / 'contracts/runtime'
    values = {}
    for name, digest in RC0_SHA256.items():
        try:
            raw = operations_raw if name == 'runtime-operations-v1.json' and operations_raw is not None else (root / name).read_bytes()
        except OSError:
            raise Refusal('contract_unavailable') from None
        if type(raw) is not bytes or sha256(raw) != digest:
            raise Refusal('operations_mismatch' if name == 'runtime-operations-v1.json' else 'contract_mismatch')
        values[name] = decode(raw, limit=MAX_LOCK_BYTES)
    return RuntimeContracts(Draft202012Validator(values['runtime-lock-v1.schema.json']),
                            Draft202012Validator(values['runtime-config-v1.schema.json']),
                            values['runtime-operations-v1.json'], RC0_SHA256['runtime-operations-v1.json'])
