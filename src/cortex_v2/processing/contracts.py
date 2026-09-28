"""Transport-free contracts for the Processing module.

Every port (document parser, embedder, blob store) is expressed here as plain
dataclasses and Protocols, so domain code never imports FastAPI, MCP or CLI
formatting and remote inference stays an outward port. Outcomes are typed: a
missing executor, an encrypted document, a provider outage and a permanent
parse failure are distinct results — never a silent success and never an empty
"no data" answer.

Executor roles follow the agreed v2 container naming plan: ``doc`` is the
Document Processor (multi-format, not a renamed PDF worker), ``embed`` is the
embedding worker, ``graph`` is the code/memory graph worker owned by Retrieval.
One application package runs as any role via
``python -m cortex_v2.worker --role {doc,embed,graph}``; each role claims only
its own job kinds and each container carries only its own dependency set.

Disposition vocabulary shared by the queue and the worker:

``retry``      transient; requeue with bounded backoff
``quarantine`` permanent input/schema failure; payload preserved for review
``block``      missing capability or configuration; visible, resumable later
``fail``       attempts exhausted or cancelled; typed error retained on the job
``succeed``    published a projection effect under a live fencing epoch
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable
from uuid import UUID

SCHEMA_VERSION = 1

DISPOSITION_RETRY = "retry"
DISPOSITION_QUARANTINE = "quarantine"
DISPOSITION_BLOCK = "block"
DISPOSITION_FAIL = "fail"
DISPOSITION_SUCCEED = "succeed"

#: Executor roles. ``core`` work (pure-stdlib parsing, embedding, transforms)
#: runs in the ``embed`` role container; ``doc`` is the optional isolated
#: Document Processor with its own pre-approved pure-Python dependency set;
#: ``image``/``audio``/``video`` are optional media roles that exist only when
#: installed and activated. A job names the executor role it requires and only
#: a worker serving that role may claim it.
EXECUTOR_CORE = "core"
EXECUTOR_DOC = "doc"
EXECUTOR_IMAGE = "image"
EXECUTOR_AUDIO = "audio"
EXECUTOR_VIDEO = "video"
#: ``graph`` is the code/memory graph executor role owned by Retrieval; its jobs
#: pin it explicitly so only a graph worker claims them.
EXECUTOR_GRAPH = "graph"
EXECUTOR_ROLES = (
    EXECUTOR_CORE,
    EXECUTOR_DOC,
    EXECUTOR_GRAPH,
    EXECUTOR_IMAGE,
    EXECUTOR_AUDIO,
    EXECUTOR_VIDEO,
)

#: Worker roles accepted by ``python -m cortex_v2.worker --role``.
ROLE_DOC = "doc"
ROLE_EMBED = "embed"
ROLE_GRAPH = "graph"
WORKER_ROLES = (ROLE_DOC, ROLE_EMBED, ROLE_GRAPH)

#: Durable job kinds. ``doc.extract`` covers every document format the Document
#: Processor serves; the graph kinds are registered by the Retrieval module
#: through :func:`cortex_v2.processing.registry.register_job_handler`.
JOB_DOC_EXTRACT = "doc.extract"
JOB_EMBED_CHUNKS = "embed.chunks"
JOB_TRANSFORM_DISTILL = "transform.distill"
JOB_TRANSFORM_COMPACT = "transform.compact"
JOB_GRAPH_CODE_EXTRACT = "graph.code.extract"
JOB_GRAPH_MEMORY_EXTRACT = "graph.memory.extract"
JOB_KINDS = (
    JOB_DOC_EXTRACT,
    JOB_EMBED_CHUNKS,
    JOB_TRANSFORM_DISTILL,
    JOB_TRANSFORM_COMPACT,
    JOB_GRAPH_CODE_EXTRACT,
    JOB_GRAPH_MEMORY_EXTRACT,
)

#: Job kinds served by the pure-stdlib core of this package.
CORE_JOB_KINDS = (
    JOB_DOC_EXTRACT,
    JOB_EMBED_CHUNKS,
    JOB_TRANSFORM_DISTILL,
    JOB_TRANSFORM_COMPACT,
)


class Outcome(str, enum.Enum):
    """Typed execution outcomes. The value is the persisted ``error_code``."""

    OK = "ok"
    #: The format is not served by any installed executor.
    UNSUPPORTED_FORMAT = "unsupported_format"
    #: The executor role that serves this format is not installed/activated.
    EXECUTOR_NOT_ACTIVATED = "executor_not_activated"
    #: The executor role is installed but not reachable right now.
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    #: The document needs optical extraction (scanned PDF, image-only page).
    OCR_NEEDED = "ocr_needed"
    #: Bytes are password protected or otherwise sealed.
    INPUT_ENCRYPTED = "input_encrypted"
    #: Container/signature is valid but the payload is damaged.
    INPUT_CORRUPT = "input_corrupt"
    #: Parsed cleanly, contained no extractable text; zero chunks is honest.
    INPUT_EMPTY = "input_empty"
    #: Caller-supplied bytes/text violate the declared contract.
    INPUT_REJECTED = "input_rejected"
    #: A parser quota (size, depth, decompression ratio, page count) was hit.
    QUOTA_EXCEEDED = "quota_exceeded"
    #: The referenced original bytes could not be resolved.
    SOURCE_BYTES_UNAVAILABLE = "source_bytes_unavailable"
    #: Transient executor/IO failure; a retry may succeed.
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_DEGRADED = "provider_degraded"
    CONFIGURATION_MISSING = "configuration_missing"
    SPACE_MISMATCH = "space_mismatch"
    OUTPUT_INVALID = "output_invalid"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


#: Outcome -> queue disposition. Complete by construction: every ``Outcome``
#: member appears exactly once, which the unit suite asserts.
OUTCOME_DISPOSITIONS: dict[Outcome, str] = {
    Outcome.OK: DISPOSITION_SUCCEED,
    Outcome.INPUT_EMPTY: DISPOSITION_SUCCEED,
    Outcome.UNSUPPORTED_FORMAT: DISPOSITION_BLOCK,
    Outcome.EXECUTOR_NOT_ACTIVATED: DISPOSITION_BLOCK,
    Outcome.EXECUTOR_UNAVAILABLE: DISPOSITION_BLOCK,
    Outcome.OCR_NEEDED: DISPOSITION_BLOCK,
    Outcome.INPUT_ENCRYPTED: DISPOSITION_BLOCK,
    Outcome.SOURCE_BYTES_UNAVAILABLE: DISPOSITION_BLOCK,
    Outcome.CONFIGURATION_MISSING: DISPOSITION_BLOCK,
    Outcome.SPACE_MISMATCH: DISPOSITION_BLOCK,
    Outcome.INPUT_CORRUPT: DISPOSITION_QUARANTINE,
    Outcome.INPUT_REJECTED: DISPOSITION_QUARANTINE,
    Outcome.QUOTA_EXCEEDED: DISPOSITION_QUARANTINE,
    Outcome.PROVIDER_REJECTED: DISPOSITION_QUARANTINE,
    Outcome.OUTPUT_INVALID: DISPOSITION_QUARANTINE,
    Outcome.TEMPORARILY_UNAVAILABLE: DISPOSITION_RETRY,
    Outcome.PROVIDER_UNAVAILABLE: DISPOSITION_RETRY,
    Outcome.PROVIDER_TIMEOUT: DISPOSITION_RETRY,
    Outcome.PROVIDER_DEGRADED: DISPOSITION_RETRY,
    Outcome.INTERNAL_ERROR: DISPOSITION_RETRY,
    Outcome.CANCELLED: DISPOSITION_FAIL,
}

#: Outcomes that never consume another attempt silently: they surface as
#: explicit blocked/quarantined/failed states (R12/R13, F06).
TERMINAL_OUTCOMES = frozenset(
    outcome
    for outcome, disposition in OUTCOME_DISPOSITIONS.items()
    if disposition in (DISPOSITION_QUARANTINE, DISPOSITION_BLOCK, DISPOSITION_FAIL)
)


def disposition_for(outcome: Outcome) -> str:
    return OUTCOME_DISPOSITIONS[outcome]


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """Bounded parser quotas (N04): no unbounded RAM, input or fan-out."""

    max_input_chars: int = 1_000_000
    max_input_bytes: int = 32_000_000
    max_blocks: int = 20_000
    max_block_chars: int = 65_536
    max_depth: int = 64
    max_pages: int = 2_000
    max_rows: int = 200_000
    max_attachments: int = 32
    max_decompression_ratio: int = 64
    max_values_indexed: int = 5_000

    def violations(self) -> tuple[str, ...]:
        bounds = {
            "max_input_chars": (1, 8_000_000),
            "max_input_bytes": (1, 256_000_000),
            "max_blocks": (1, 200_000),
            "max_block_chars": (1, 65_536),
            "max_depth": (1, 256),
            "max_pages": (1, 20_000),
            "max_rows": (1, 2_000_000),
            "max_attachments": (0, 1_024),
            "max_decompression_ratio": (1, 1_024),
            "max_values_indexed": (0, 200_000),
        }
        problems: list[str] = []
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not isinstance(value, int) or not low <= value <= high:
                problems.append(name)
        return tuple(problems)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One canonical content revision handed to a parser.

    ``body_text`` is the preserved original text of the revision and ``payload``
    its typed W1 payload; parsers never mutate either and never receive
    credentials, host paths or ambient configuration. ``body_bytes`` is present
    only when a blob resolver returned the referenced original bytes, which
    binary formats require.
    """

    scope_id: UUID
    content_id: UUID
    revision: int
    content_class: str
    media_type: str
    body_text: str
    content_hash: bytes
    payload: dict[str, Any] = field(default_factory=dict)
    filename: str | None = None
    body_bytes: bytes | None = None
    size_bytes: int | None = None

    @property
    def identity(self) -> str:
        return f"{self.scope_id}:{self.content_id}:{self.revision}"


@dataclass(frozen=True, slots=True)
class Span:
    """Exact provenance for a slice of an original (R12).

    For text formats ``start``/``end`` are character offsets into
    ``SourceRef.body_text`` and a verbatim format must satisfy
    ``body_text[start:end] == block.text`` exactly. For binary formats (PDF,
    OOXML, OpenDocument, EPUB, RTF) they index the parser's own extracted text
    stream, and the exact location in the original is carried by the locator
    fields (``page``/``part``/``sheet``/``cell``/``slide``/``row``) plus
    ``detail`` — a citation is always re-verifiable against the preserved
    original bytes one way or the other. Optional locators add
    page/line/sheet/cell/slide/JSON-pointer/symbol context where the parser can
    observe it; they never replace the offsets.
    """

    start: int
    end: int
    kind: str = "text_offset"
    line_start: int | None = None
    line_end: int | None = None
    page: int | None = None
    sheet: str | None = None
    cell: str | None = None
    slide: int | None = None
    row: int | None = None
    json_pointer: str | None = None
    symbol: str | None = None
    part: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("span offsets must be ordered and non-negative")

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "start": self.start, "end": self.end}
        for name in (
            "line_start",
            "line_end",
            "page",
            "sheet",
            "cell",
            "slide",
            "row",
            "json_pointer",
            "symbol",
            "part",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = item
        if self.detail:
            value["detail"] = dict(self.detail)
        return value


@dataclass(frozen=True, slots=True)
class ParseBlock:
    """A structural unit with exact provenance, produced by a parser."""

    block_kind: str
    text: str
    span: Span
    heading_path: tuple[str, ...] = ()
    language: str | None = None
    weight: int = 1


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Typed parser/executor outcome.

    ``executor`` names the implementation and version that produced the result
    (``parser.markdown@1``), which projection rows persist so an extracted
    passage is tied to an exact extractor revision (R12). ``detected_by``
    records how the format was identified: ``signature``, ``media_type``,
    ``extension`` or ``content`` — detection never trusts a caller assertion
    alone.
    """

    outcome: Outcome
    blocks: tuple[ParseBlock, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    executor: str | None = None
    format_id: str | None = None
    detected_by: str | None = None
    required_role: str | None = None
    detail: str | None = None
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """Pinned chunking policy identity plus its parameters."""

    policy_id: str
    version: int
    kind: str
    target_chars: int
    max_chars: int
    overlap_chars: int

    @property
    def identity(self) -> str:
        return f"{self.policy_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk of one content revision inside one embedding space."""

    ordinal: int
    text: str
    span: Span
    block_spans: tuple[Span, ...]
    block_kinds: tuple[str, ...]
    text_sha256: bytes
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """A chunk revision read back from the store for embedding."""

    chunk_id: UUID
    ordinal: int
    text: str
    span: dict[str, Any]
    block_kinds: tuple[str, ...]
    text_sha256: bytes


@dataclass(frozen=True, slots=True)
class SpaceContract:
    """Immutable embedding-space semantics a provider must honour (F07)."""

    space_id: UUID
    space_name: str
    provider: str
    model_id: str
    model_revision: str | None
    dimensions: int
    normalization: str
    metric: str
    query_prefix: str | None = None
    document_prefix: str | None = None
    #: ``<policy_id>@<version>`` of the chunking policy this space was built
    #: with. A profile whose chunking policy differs is incompatible with the
    #: space, and the mismatch is a typed outcome rather than a silent rebuild.
    chunking_identity: str | None = None

    def document_text(self, text: str) -> str:
        return f"{self.document_prefix}{text}" if self.document_prefix else text

    def query_text(self, text: str) -> str:
        return f"{self.query_prefix}{text}" if self.query_prefix else text


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Declared adapter capabilities, batching and locality (§7)."""

    provider_id: str
    locality: str
    roles: tuple[str, ...]
    max_batch: int
    max_input_chars: int
    dimensions: tuple[int, ...] = ()
    supports_cancellation: bool = True
    reports_usage: bool = False
    offline: bool = False
    models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmbeddingOutcome:
    """Typed embedder result; ``vectors`` is empty unless outcome is OK."""

    outcome: Outcome
    vectors: tuple[tuple[float, ...], ...] = ()
    model_revision: str | None = None
    detail: str | None = None
    retry_after_seconds: float | None = None
    degraded: tuple[str, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@runtime_checkable
class Parser(Protocol):
    """Extractor port: one source revision -> blocks with exact provenance."""

    parser_id: str
    format_ids: tuple[str, ...]

    def parse(self, source: SourceRef, limits: ParserLimits) -> ParseResult:
        """Pure, synchronous and bounded; runs in a worker thread executor."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding port: space + ordered batch + deadline -> vectors/errors."""

    provider_id: str
    locality: str

    def capabilities(self) -> ProviderCapabilities:
        """Declare batching/input limits, locality and cancellation support."""

    async def embed_documents(
        self,
        space: SpaceContract,
        texts: tuple[str, ...],
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        """Embed an ordered document batch; order and count must be preserved."""

    async def embed_query(
        self,
        space: SpaceContract,
        text: str,
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        """Embed one interactive query using the space's query prefix."""


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """Everything a job handler may know about the attempt it is running.

    The fencing epoch and lease owner are carried so a handler can include them
    in its own effect keys; publication is performed by the worker, never by the
    handler, and only under a re-checked epoch.
    """

    job_id: UUID
    job_kind: str
    scope_id: UUID
    installation_id: UUID
    principal_id: UUID
    fencing_epoch: int
    lease_owner: str
    attempt_id: UUID
    role: str
    intent: dict[str, Any]
    deadline: float
    attempt_number: int = 1
    #: Pins of a content-processing job; all None for graph kinds, which carry
    #: their own identity in ``intent["payload"]["pin"]``.
    content_id: UUID | None = None
    source_revision: int | None = None
    profile_id: UUID | None = None
    space_id: UUID | None = None
    generation_id: UUID | None = None


@runtime_checkable
class AttemptServices(Protocol):
    """Narrow services a handler may call. No connection is ever exposed.

    Every method opens and closes its own short transaction, which is what makes
    "never hold a database transaction across model/network/filesystem
    execution" a structural property instead of a code-review hope (F08).
    """

    role: str

    async def read_source(self, context: AttemptContext) -> SourceRef | None:
        """Load the pinned canonical revision, or None when it disappeared."""

    async def read_bytes(self, source: SourceRef) -> bytes | None:
        """Resolve referenced original bytes through the configured blob port."""

    async def read_space(self, space_id: UUID) -> SpaceContract | None:
        """Load immutable space semantics plus its active/building generation."""

    async def read_chunks(
        self, context: AttemptContext, space_id: UUID
    ) -> tuple[StoredChunk, ...]:
        """Load published chunk revisions for the pinned source revision."""

    async def read_profile(self, profile_id: UUID) -> dict[str, Any] | None:
        """Load the immutable processing profile pinned by the job."""

    def parser_for(self, format_id: str) -> Parser | None:
        """Return the parser this worker role can actually execute."""

    def embedder_for(self, space: SpaceContract) -> EmbeddingProvider | None:
        """Return an embedder allowed by routing policy, or None."""

    async def run_blocking(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run bounded CPU work in the worker's thread executor."""


JobHandler = Callable[[AttemptContext, AttemptServices], Awaitable["ExecutionReport"]]


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Result of one attempt, handed to the queue for a fenced decision."""

    outcome: Outcome
    chunks: tuple[Chunk, ...] = ()
    vectors: tuple[tuple[float, ...], ...] = ()
    vector_chunk_ids: tuple[UUID, ...] = ()
    distillation: dict[str, Any] | None = None
    #: Module-owned structured output handed to the registered publisher inside
    #: the fenced publish transaction. ``distillation`` stays reserved for the
    #: R18 transform kinds; other modules (for example Retrieval's graph
    #: extraction) carry their payload here instead of overloading it.
    handler_output: dict[str, Any] | None = None
    executor: str | None = None
    format_id: str | None = None
    detected_by: str | None = None
    #: Executor role this work actually needs, when execution discovered a
    #: different one than enqueue-time detection predicted. The queue reroutes
    #: the job at most once instead of blocking it.
    required_role: str | None = None
    model_revision: str | None = None
    detail: str | None = None
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    retry_after_seconds: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def disposition(self) -> str:
        return disposition_for(self.outcome)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK
