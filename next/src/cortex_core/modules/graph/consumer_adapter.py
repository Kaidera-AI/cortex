"""Graph fact binding; never re-encode Nemo's extraction bytes."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class GraphFactBinding:
    payload: bytes
    source_kind: str
    revision: int
    extractor: str


def bind_graph_fact(payload: bytes, *, source_kind: str, revision: int, extractor: str):
    if not isinstance(payload, bytes) or not payload.startswith(b'{"source_kind":'):
        raise ValueError('invalid_graph_fact')
    try:
        fact = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('invalid_graph_fact') from error
    if (fact.get('source_kind') != source_kind or fact.get('identity') != extractor
            or not isinstance(revision, int) or revision < 1
            or not isinstance(fact.get('nodes'), list) or not isinstance(fact.get('edges'), list)):
        raise ValueError('invalid_graph_fact')
    return GraphFactBinding(payload, source_kind, revision, extractor)
