"""Versioned C07 consumer and durable target receipt contracts."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModuleManifest:
    module_id: str
    protocol_version: int
    schema_version: int
    aggregate_kinds: tuple[str, ...]
    sink: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {
            'module_id', 'protocol_version', 'schema_version', 'aggregate_kinds', 'sink'
        }:
            raise ValueError('invalid_manifest')
        module_id = value['module_id']
        kinds = value['aggregate_kinds']
        if (not isinstance(module_id, str) or not 1 <= len(module_id) <= 128
                or value['protocol_version'] != 1 or value['schema_version'] != 1
                or value['sink'] != 'postgres' or not isinstance(kinds, list)
                or not kinds or any(not isinstance(kind, str) or not kind for kind in kinds)):
            raise ValueError('invalid_manifest')
        return cls(module_id, 1, 1, tuple(kinds), 'postgres')


class ModuleSink(Protocol):
    def apply(self, event, generation: int) -> dict:
        """Commit effect, revision and tombstone atomically before returning."""
