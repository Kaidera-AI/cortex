"""Pinned RC0 wire contract; reuse R01 decoding, never a second declaration ABI."""
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from cortex_core.release.codec import decode_json, sha256
from .signed_member import Refusal

MAX_LOCK_BYTES = 262144
MAX_CONFIG_BYTES = 65536
RC0_SHA256 = {
    'runtime-lock-v1.schema.json': '82cc5af6ab758e8fc51ada65e3cb8ec574f6e7bf8960802849c58280b07463aa',
    'runtime-config-v1.schema.json': '5ea3feaca027ad2a9aab6c280ba33194ae7a9e686916bd8056d9b08f0e9f4e7b',
    'runtime-operations-v1.json': '51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b',
}


def decode(raw: bytes, *, limit: int, text_payload=False):
    if type(raw) is not bytes or len(raw) > limit:
        raise Refusal('raw_limit')
    try:
        value = decode_json(raw, limit, canonical=False, text_payload=text_payload)
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
    member_sizes: dict


def load_contracts(*, operations_raw: bytes | None = None) -> RuntimeContracts:
    """Read only this package's fixed RC0 members, after signed-member admission.

    The optional operation bytes support an explicit transport, not policy:
    byte-for-byte pinning applies equally to both sources. No schema URL fetches.
    """
    root = Path(__file__).resolve().parents[3] / 'contracts/runtime'
    values = {}; sizes = {}
    for name, digest in RC0_SHA256.items():
        try:
            raw = operations_raw if name == 'runtime-operations-v1.json' and operations_raw is not None else (root / name).read_bytes()
        except OSError:
            raise Refusal('contract_unavailable') from None
        if type(raw) is not bytes or sha256(raw) != digest:
            raise Refusal('operations_mismatch' if name == 'runtime-operations-v1.json' else 'contract_mismatch')
        values[name] = decode(raw, limit=MAX_LOCK_BYTES)
        sizes[name] = len(raw)
    return RuntimeContracts(Draft202012Validator(values['runtime-lock-v1.schema.json']),
                            Draft202012Validator(values['runtime-config-v1.schema.json']),
                            values['runtime-operations-v1.json'], RC0_SHA256['runtime-operations-v1.json'], sizes)
