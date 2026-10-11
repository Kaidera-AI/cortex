"""Immutable, distinct build decisions. Neither decision grants effects."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ParsedDocument(Mapping):
    raw: bytes
    sha256: str
    document: Mapping

    def __getitem__(self, key):
        return self.document[key]

    def __iter__(self):
        return iter(self.document)

    def __len__(self):
        return len(self.document)


class ParsedBuildInput(ParsedDocument):
    pass


class ParsedBuildReceipt(ParsedDocument):
    pass


class BuildRefusal(ValueError):
    """Stable refusal reason, raised before a builder can receive admission."""


class ReceiptRefusal(ValueError):
    """Stable refusal reason, raised before assembly can receive verification."""


_SEAL = object()


@dataclass(frozen=True)
class VerifiedBuildInput:
    input_sha256: str
    target: Mapping
    inventory: tuple
    requirements: Mapping
    document: Mapping
    native_qualified: bool
    _seal: object


@dataclass(frozen=True)
class VerifiedBuildReceipt:
    receipt_sha256: str
    input_sha256: str
    target: Mapping
    output_sha256: str
    evidence_digests: Mapping
    native_qualified: bool
    document: Mapping
    _seal: object
