"""Parser registry with per-adapter dependency guards and typed dispatch.

Adapters live in sibling modules and are imported defensively: a worker image
without the Document Processor's optional dependencies still starts, still
serves every pure-stdlib format, and reports the remaining formats as typed
``executor_not_activated`` results instead of raising ImportError at boot or
silently returning empty text.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from ..contracts import (
    EXECUTOR_AUDIO,
    EXECUTOR_IMAGE,
    EXECUTOR_VIDEO,
    Outcome,
    ParseResult,
    Parser,
    ParserLimits,
    SourceRef,
)
from ..formats import (
    ARCHIVE,
    OLE_LEGACY,
    UNKNOWN_BINARY,
    ZIP_CONTAINER,
    Detection,
    detect_format,
    required_role_for,
    spec_for,
)

#: Adapter module -> the format ids it claims. Every module exposes
#: ``PARSERS: dict[str, Parser]`` and may expose ``DEPENDENCIES: dict[str, str]``
#: mapping a format id to the distribution it needs. Optional dependencies are
#: imported *inside* ``parse`` and reported as a typed outcome, so one missing
#: package never disables the stdlib formats that live beside it.
ADAPTER_MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("text", ("text", "source_code", "unknown_text")),
    ("markup", ("markdown", "html")),
    ("rtf", ("rtf",)),
    ("delimited", ("csv", "tsv")),
    ("json", ("json", "jsonl")),
    ("yaml", ("yaml",)),
    ("xml", ("xml",)),
    ("office", ("docx", "pptx", "xlsx", "odt", "odp", "ods", "epub", ZIP_CONTAINER)),
    ("binary", (OLE_LEGACY, ARCHIVE, UNKNOWN_BINARY, "image", "audio", "video")),
    ("mail", ("eml", "mbox")),
    ("pdf", ("pdf",)),
)


#: Formats a parser can serve from ``SourceRef.body_text`` alone. Every other
#: format needs the referenced original bytes; when they cannot be resolved the
#: result is a typed ``source_bytes_unavailable`` outcome, never empty text.
TEXT_FORMATS = frozenset(
    {
        "text",
        "markdown",
        "html",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "source_code",
        "eml",
        "mbox",
        "unknown_text",
    }
)

_MEDIA_ROLES = (EXECUTOR_IMAGE, EXECUTOR_AUDIO, EXECUTOR_VIDEO)


@dataclass(frozen=True, slots=True)
class AdapterState:
    """Load state of one adapter module."""

    module: str
    formats: tuple[str, ...]
    dependencies: dict[str, str]
    import_error: str | None = None

    @property
    def available(self) -> bool:
        return self.import_error is None


def _load_adapters() -> tuple[dict[str, AdapterState], dict[str, Parser], dict[str, str]]:
    states: dict[str, AdapterState] = {}
    parsers: dict[str, Parser] = {}
    dependencies: dict[str, str] = {}
    for module_name, format_ids in ADAPTER_MODULES:
        try:
            module = import_module(f"{__name__}.{module_name}")
        except ImportError as exc:  # defensive: adapters must not import deps eagerly
            missing = exc.name or module_name
            states[module_name] = AdapterState(
                module=module_name,
                formats=format_ids,
                dependencies={},
                import_error=f"{missing}: {exc}",
            )
            continue
        declared: dict[str, Parser] = dict(getattr(module, "PARSERS", {}))
        module_dependencies: dict[str, str] = dict(getattr(module, "DEPENDENCIES", {}))
        states[module_name] = AdapterState(
            module=module_name,
            formats=tuple(declared) or format_ids,
            dependencies=module_dependencies,
        )
        dependencies.update(module_dependencies)
        for format_id, parser in declared.items():
            parsers.setdefault(format_id, parser)
    return states, parsers, dependencies


ADAPTER_STATES, PARSERS, FORMAT_DEPENDENCIES = _load_adapters()


def parser_for(format_id: str | None) -> Parser | None:
    """Return an executable parser for a format in *this* worker image."""
    if not format_id:
        return None
    return PARSERS.get(format_id)


def declared_dependency(format_id: str | None) -> str | None:
    """Distribution a format needs, when its adapter declared one."""
    if not format_id:
        return None
    return FORMAT_DEPENDENCIES.get(format_id)


def registry_report() -> dict[str, object]:
    """Observable adapter state for discovery and health reporting."""
    return {
        "available": sorted(PARSERS),
        "dependencies": {
            format_id: FORMAT_DEPENDENCIES[format_id]
            for format_id in sorted(FORMAT_DEPENDENCIES)
        },
        "adapters": [
            {
                "module": state.module,
                "formats": list(state.formats),
                "available": state.available,
                "import_error": state.import_error,
            }
            for state in ADAPTER_STATES.values()
        ],
    }


def _outcome_for_spec(detection: Detection, source: SourceRef) -> ParseResult:
    """Typed result for a format this worker cannot or must not parse."""
    spec = detection.spec
    format_id = detection.format_id or UNKNOWN_BINARY
    if spec is None:
        return ParseResult(
            outcome=Outcome.UNSUPPORTED_FORMAT,
            format_id=format_id,
            detected_by=detection.detected_by,
            detail=detection.reason or "format could not be identified",
        )
    if spec.status == "unsupported":
        return ParseResult(
            outcome=Outcome.UNSUPPORTED_FORMAT,
            format_id=spec.format_id,
            detected_by=detection.detected_by,
            detail=spec.notes or "format is not supported by any executor",
        )
    if spec.roles and spec.roles[0] in _MEDIA_ROLES:
        return ParseResult(
            outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
            format_id=spec.format_id,
            detected_by=detection.detected_by,
            required_role=spec.roles[0],
            detail=(
                f"{spec.label} needs the optional {spec.roles[0]} executor role, "
                "which is not installed or not activated"
            ),
        )
    reason = declared_dependency(spec.format_id)
    if parser_for(spec.format_id) is None:
        return ParseResult(
            outcome=Outcome.EXECUTOR_NOT_ACTIVATED,
            format_id=spec.format_id,
            detected_by=detection.detected_by,
            required_role=required_role_for(spec.format_id),
            detail=reason or "no parser is registered for this format in this worker",
        )
    if not spec.parser_id:
        return ParseResult(
            outcome=Outcome.UNSUPPORTED_FORMAT,
            format_id=spec.format_id,
            detected_by=detection.detected_by,
            detail="format has no parser implementation",
        )
    if source.body_bytes is None and spec.format_id not in TEXT_FORMATS:
        return ParseResult(
            outcome=Outcome.SOURCE_BYTES_UNAVAILABLE,
            format_id=spec.format_id,
            detected_by=detection.detected_by,
            required_role=required_role_for(spec.format_id),
            detail="the referenced original bytes could not be resolved",
        )
    return ParseResult(
        outcome=Outcome.UNSUPPORTED_FORMAT,
        format_id=spec.format_id,
        detected_by=detection.detected_by,
        detail="format cannot be parsed from the supplied content",
    )


def dispatch(
    source: SourceRef,
    *,
    limits: ParserLimits | None = None,
    head: bytes | None = None,
) -> ParseResult:
    """Detect the format and run the matching parser, or return a typed outcome.

    This function is synchronous and bounded; callers run it inside the worker's
    thread executor. It never raises for an unsupported, encrypted, corrupt or
    media-only input — those are typed results.
    """
    active_limits = limits or ParserLimits()
    problems = active_limits.violations()
    if problems:
        return ParseResult(
            outcome=Outcome.INPUT_REJECTED,
            detail=f"invalid parser limits: {', '.join(problems)}",
        )
    detection = detect_format(
        media_type=source.media_type,
        filename=source.filename,
        head=head if head is not None else source.body_bytes[:64] if source.body_bytes else None,
        body_text=source.body_text or None,
    )
    parser = parser_for(detection.format_id)
    if parser is None:
        return _outcome_for_spec(detection, source)
    try:
        result = parser.parse(source, active_limits)
    except RecursionError:
        return ParseResult(
            outcome=Outcome.QUOTA_EXCEEDED,
            format_id=detection.format_id,
            detected_by=detection.detected_by,
            executor=getattr(parser, "parser_id", None),
            detail="parser nesting exceeded the recursion budget",
        )
    except (MemoryError, OSError) as exc:
        return ParseResult(
            outcome=Outcome.TEMPORARILY_UNAVAILABLE,
            format_id=detection.format_id,
            detected_by=detection.detected_by,
            executor=getattr(parser, "parser_id", None),
            detail=f"{type(exc).__name__} while reading the source",
        )
    except (ValueError, KeyError, IndexError, UnicodeDecodeError) as exc:
        return ParseResult(
            outcome=Outcome.INPUT_CORRUPT,
            format_id=detection.format_id,
            detected_by=detection.detected_by,
            executor=getattr(parser, "parser_id", None),
            detail=f"{type(exc).__name__}: {exc}"[:512],
        )
    if result.format_id is None:
        result = ParseResult(
            outcome=result.outcome,
            blocks=result.blocks,
            warnings=result.warnings,
            truncated=result.truncated,
            executor=result.executor or getattr(parser, "parser_id", None),
            format_id=detection.format_id,
            detected_by=result.detected_by or detection.detected_by,
            required_role=result.required_role,
            detail=result.detail,
            stats=result.stats,
        )
    if result.outcome is Outcome.OK:
        # Parsers own truncation and quota enforcement; this only catches an
        # adapter that violated its contract, which would otherwise push
        # unbounded rows into the store (N04).
        violation = None
        if len(result.blocks) > active_limits.max_blocks:
            violation = "block_count_over_limit"
        elif any(len(block.text) > active_limits.max_block_chars for block in result.blocks):
            violation = "block_chars_over_limit"
        elif any(
            block.span.end > len(source.body_text) + len(source.body_bytes or b"")
            for block in result.blocks
        ):
            violation = "span_out_of_range"
        if violation is not None:
            return ParseResult(
                outcome=Outcome.OUTPUT_INVALID,
                warnings=(*result.warnings, violation),
                executor=result.executor,
                format_id=result.format_id,
                detected_by=result.detected_by,
                detail=f"parser violated its limits contract: {violation}",
                stats=result.stats,
            )
    return result


__all__ = [
    "ADAPTER_MODULES",
    "ADAPTER_STATES",
    "FORMAT_DEPENDENCIES",
    "PARSERS",
    "TEXT_FORMATS",
    "declared_dependency",
    "dispatch",
    "parser_for",
    "registry_report",
]
