"""Pure unit tests for the Processing queue, registry, formats and contracts.

No database, no network, no model downloads: these exercise the durable-work
logic, the typed outcome vocabulary, format detection precedence, blob
confinement, request-model boundaries and the published operation contract.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import uuid

import pytest
from pydantic import BaseModel

from cortex_v2.processing import formats, keys, parsers, queue
from cortex_v2.processing.blobs import (
    FilesystemBlobResolver,
    NullBlobResolver,
    expected_digest,
    resolver_from_root,
)
from cortex_v2.processing.contracts import (
    DISPOSITION_BLOCK,
    DISPOSITION_FAIL,
    DISPOSITION_QUARANTINE,
    DISPOSITION_RETRY,
    DISPOSITION_SUCCEED,
    EXECUTOR_ROLES,
    JOB_KINDS,
    OUTCOME_DISPOSITIONS,
    WORKER_ROLES,
    AttemptContext,
    ChunkingPolicy,
    ExecutionReport,
    Outcome,
    ParserLimits,
    SourceRef,
    Span,
    SpaceContract,
    disposition_for,
)
from cortex_v2.processing.models import (
    BackfillRequest,
    ChunkingPolicyInput,
    CreateEmbeddingSpaceRequest,
    ListJobsRequest,
)
from cortex_v2.processing.operations import OPERATIONS
from cortex_v2.processing.queue import JobIntent
from cortex_v2.processing.registry import (
    JOB_REGISTRY,
    JobRegistry,
    RegistryConflict,
    RegistryError,
)
from cortex_v2.store import ApiProblem

SCOPE = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
SPACE = uuid.UUID("33333333-3333-4333-8333-333333333333")
GENERATION = uuid.UUID("44444444-4444-4444-8444-444444444444")
PROFILE = uuid.UUID("c2a00000-0000-4000-8000-000000000001")


def source_ref(body: str, *, media_type: str = "text/plain", filename: str | None = None,
               payload: dict | None = None) -> SourceRef:
    return SourceRef(
        scope_id=SCOPE,
        content_id=CONTENT,
        revision=1,
        content_class="knowledge",
        media_type=media_type,
        body_text=body,
        content_hash=b"\x00" * 32,
        payload=payload or {},
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Typed outcome vocabulary
# ---------------------------------------------------------------------------


def test_every_outcome_has_exactly_one_disposition():
    assert set(OUTCOME_DISPOSITIONS) == set(Outcome)
    known = {
        DISPOSITION_RETRY,
        DISPOSITION_QUARANTINE,
        DISPOSITION_BLOCK,
        DISPOSITION_FAIL,
        DISPOSITION_SUCCEED,
    }
    assert set(OUTCOME_DISPOSITIONS.values()) <= known


def test_transient_outcomes_retry_and_permanent_ones_do_not():
    assert disposition_for(Outcome.PROVIDER_TIMEOUT) == DISPOSITION_RETRY
    assert disposition_for(Outcome.PROVIDER_UNAVAILABLE) == DISPOSITION_RETRY
    assert disposition_for(Outcome.TEMPORARILY_UNAVAILABLE) == DISPOSITION_RETRY
    assert disposition_for(Outcome.INPUT_CORRUPT) == DISPOSITION_QUARANTINE
    assert disposition_for(Outcome.QUOTA_EXCEEDED) == DISPOSITION_QUARANTINE
    assert disposition_for(Outcome.OCR_NEEDED) == DISPOSITION_BLOCK
    assert disposition_for(Outcome.EXECUTOR_NOT_ACTIVATED) == DISPOSITION_BLOCK
    assert disposition_for(Outcome.INPUT_ENCRYPTED) == DISPOSITION_BLOCK
    assert disposition_for(Outcome.CANCELLED) == DISPOSITION_FAIL


def test_empty_extraction_is_honest_success_not_a_hidden_failure():
    assert disposition_for(Outcome.INPUT_EMPTY) == DISPOSITION_SUCCEED
    report = ExecutionReport(outcome=Outcome.INPUT_EMPTY)
    assert report.disposition == DISPOSITION_SUCCEED
    assert not report.ok


def test_span_rejects_impossible_offsets_and_omits_empty_locators():
    with pytest.raises(ValueError):
        Span(start=5, end=4)
    with pytest.raises(ValueError):
        Span(start=-1, end=2)
    assert Span(start=0, end=3).as_dict() == {"kind": "text_offset", "start": 0, "end": 3}
    assert Span(start=0, end=3, page=7, cell="B2").as_dict()["page"] == 7


def test_space_contract_applies_prefixes_only_when_declared():
    space = SpaceContract(
        space_id=SPACE,
        space_name="s",
        provider="hash-local",
        model_id="m",
        model_revision=None,
        dimensions=8,
        normalization="l2",
        metric="cosine",
        query_prefix="query: ",
    )
    assert space.query_text("x") == "query: x"
    assert space.document_text("x") == "x"


def test_parser_limits_validate_their_own_bounds():
    assert ParserLimits().violations() == ()
    assert "max_input_chars" in ParserLimits(max_input_chars=0).violations()
    assert "max_block_chars" in ParserLimits(max_block_chars=65_537).violations()


# ---------------------------------------------------------------------------
# Backoff, dispositions and rerouting
# ---------------------------------------------------------------------------


def test_backoff_grows_exponentially_and_stays_inside_jitter_bounds():
    delays = [
        queue.backoff_delay(attempt, base_seconds=2.0, cap_seconds=900.0, jitter_ratio=0.0)
        for attempt in range(1, 6)
    ]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]
    jittered = [
        queue.backoff_delay(3, base_seconds=2.0, cap_seconds=900.0, jitter_ratio=0.25, rng=rng)
        for rng in (lambda: 0.0, lambda: 1.0, lambda: 0.5)
    ]
    assert jittered[0] == pytest.approx(8.0 * 0.875)
    assert jittered[1] == pytest.approx(8.0 * 1.125)
    assert jittered[2] == pytest.approx(8.0)


def test_backoff_caps_and_honours_provider_retry_guidance():
    assert queue.backoff_delay(40, base_seconds=2.0, cap_seconds=900.0, jitter_ratio=0.0) == 900.0
    guided = queue.backoff_delay(
        1, base_seconds=2.0, cap_seconds=900.0, jitter_ratio=0.0, retry_after_seconds=30.0
    )
    assert guided == 30.0
    capped_guidance = queue.backoff_delay(
        1, base_seconds=2.0, cap_seconds=60.0, jitter_ratio=0.0, retry_after_seconds=5000.0
    )
    assert capped_guidance == 60.0
    assert queue.backoff_delay(0, jitter_ratio=0.0, base_seconds=2.0) == 2.0


def test_dispositions_map_onto_job_states_and_attempt_exhaustion_fails():
    assert queue._status_for(DISPOSITION_SUCCEED, attempt_number=1, max_attempts=5) == (
        "succeeded",
        True,
    )
    assert queue._status_for(DISPOSITION_RETRY, attempt_number=1, max_attempts=5) == (
        "queued",
        False,
    )
    assert queue._status_for(DISPOSITION_RETRY, attempt_number=5, max_attempts=5) == (
        "failed",
        True,
    )
    assert queue._status_for(DISPOSITION_QUARANTINE, attempt_number=1, max_attempts=5) == (
        "quarantined",
        True,
    )
    assert queue._status_for(DISPOSITION_BLOCK, attempt_number=1, max_attempts=5) == (
        "blocked",
        True,
    )


def claim(**overrides):
    base = {
        "job_id": uuid.uuid4(),
        "attempt_id": uuid.uuid4(),
        "scope_id": SCOPE,
        "installation_id": uuid.uuid4(),
        "job_kind": "doc.extract",
        "required_role": "core",
        "status": "leased",
        "content_id": CONTENT,
        "source_revision": 1,
        "profile_id": PROFILE,
        "space_id": SPACE,
        "generation_id": GENERATION,
        "batch_id": None,
        "intent": {"schema_version": 1, "payload": {}},
        "fencing_epoch": 3,
        "lease_owner": "worker-1",
        "lease_expires_at": None,
        "attempt_number": 1,
        "priority": 100,
        "max_attempts": 5,
        "rerouted_at": None,
    }
    base.update(overrides)
    return queue.ClaimedJob(**base)


def test_reroute_happens_once_and_only_for_a_real_role_change():
    blocked = ExecutionReport(outcome=Outcome.EXECUTOR_NOT_ACTIVATED, required_role="doc")
    assert queue._reroute_target(claim(), blocked, DISPOSITION_BLOCK) == "doc"
    already = claim(rerouted_at="2026-09-25T00:00:00+00:00")
    assert queue._reroute_target(already, blocked, DISPOSITION_BLOCK) is None
    same_role = claim(required_role="doc")
    assert queue._reroute_target(same_role, blocked, DISPOSITION_BLOCK) is None
    unknown = ExecutionReport(outcome=Outcome.EXECUTOR_NOT_ACTIVATED, required_role="quantum")
    assert queue._reroute_target(claim(), unknown, DISPOSITION_BLOCK) is None
    succeeded = ExecutionReport(outcome=Outcome.OK, required_role="doc")
    assert queue._reroute_target(claim(), succeeded, DISPOSITION_SUCCEED) is None


def test_attempt_status_mirrors_the_job_state_it_produced():
    assert queue._attempt_status("succeeded") == "succeeded"
    assert queue._attempt_status("queued") == "failed"
    assert queue._attempt_status("blocked") == "blocked"
    assert queue._attempt_status("quarantined") == "quarantined"


def test_cursor_parsing_round_trips_and_rejects_garbage():
    cursor = "2026-09-25T10:00:00+00:00:22222222-2222-4222-8222-222222222222"
    created_at, job_id = queue.parse_cursor(cursor)
    assert job_id == CONTENT
    assert created_at.startswith("2026-09-25T10:00:00")
    assert queue.parse_cursor(None) is None
    with pytest.raises(ApiProblem) as excinfo:
        queue.parse_cursor("not-a-cursor")
    assert excinfo.value.status == 404


# ---------------------------------------------------------------------------
# Job intents
# ---------------------------------------------------------------------------


def test_intent_validation_pins_what_each_kind_requires():
    JobIntent(
        job_kind="doc.extract",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
    ).validate()
    JobIntent(
        job_kind="graph.code.extract",
        payload={"pin": {"repository_key": "helix", "commit_sha": "abc123"}},
    ).validate()

    with pytest.raises(ApiProblem) as unknown:
        JobIntent(job_kind="not.a.kind").validate()
    assert unknown.value.code == "unsupported_job_kind"

    with pytest.raises(ApiProblem) as unpinned:
        JobIntent(job_kind="embed.chunks", content_id=CONTENT, source_revision=1).validate()
    assert unpinned.value.code == "job_intent_invalid"

    with pytest.raises(ApiProblem) as half_pinned:
        JobIntent(job_kind="doc.extract", content_id=CONTENT).validate()
    assert half_pinned.value.code == "job_intent_invalid"

    with pytest.raises(ApiProblem) as graph_without_pin:
        JobIntent(job_kind="graph.memory.extract", payload={}).validate()
    assert graph_without_pin.value.code == "job_intent_invalid"

    with pytest.raises(ApiProblem) as graph_with_profile:
        JobIntent(
            job_kind="graph.code.extract",
            profile_id=PROFILE,
            payload={"pin": {"commit_sha": "abc"}},
        ).validate()
    assert graph_with_profile.value.code == "job_intent_invalid"

    with pytest.raises(ApiProblem) as bounded:
        JobIntent(
            job_kind="doc.extract",
            content_id=CONTENT,
            source_revision=1,
            profile_id=PROFILE,
            space_id=SPACE,
            priority=5000,
        ).validate()
    assert bounded.value.code == "job_intent_invalid"


def test_dedupe_keys_are_stable_and_sensitive_to_every_pin():
    def key(**overrides) -> str:
        base: dict = {
            "job_kind": "embed.chunks",
            "content_id": CONTENT,
            "source_revision": 1,
            "profile_id": PROFILE,
            "space_id": SPACE,
            "generation_id": GENERATION,
        }
        base.update(overrides)
        return JobIntent(**base).default_dedupe_key("core.text@1", SCOPE)

    assert key() == key()
    assert key(source_revision=2) != key()
    assert key(generation_id=uuid.uuid4()) != key()
    assert key(space_id=uuid.uuid4()) != key()
    assert key(job_kind="doc.extract") != key()
    assert key(dedupe_key="explicit-key") == "explicit-key"
    # A different scope must not share a key even with identical pins.
    other_scope = JobIntent(
        job_kind="embed.chunks",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
        generation_id=GENERATION,
    ).default_dedupe_key("core.text@1", uuid.uuid4())
    assert other_scope != key()


def test_required_role_follows_the_declared_format_then_the_profile():
    profile = {"parser": {"executor_role": "core"}}
    pdf = JobIntent(
        job_kind="doc.extract",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
        payload={"media_type": "application/pdf", "filename": "report.pdf"},
    )
    assert queue.resolve_required_role(pdf, profile) == "doc"
    text = JobIntent(
        job_kind="embed.chunks",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
        generation_id=GENERATION,
        payload={"media_type": "text/plain"},
    )
    assert queue.resolve_required_role(text, profile) == "core"
    explicit = JobIntent(
        job_kind="doc.extract",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
        required_role="image",
        payload={"media_type": "text/plain"},
    )
    assert queue.resolve_required_role(explicit, profile) == "image"
    doc_profile = {"parser": {"executor_role": "doc"}}
    undeclared = JobIntent(
        job_kind="doc.extract",
        content_id=CONTENT,
        source_revision=1,
        profile_id=PROFILE,
        space_id=SPACE,
        payload={},
    )
    assert queue.resolve_required_role(undeclared, doc_profile) == "doc"


# ---------------------------------------------------------------------------
# Deterministic keys
# ---------------------------------------------------------------------------


def test_chunk_keys_are_deterministic_and_differ_by_every_identity_input():
    def key(**overrides) -> uuid.UUID:
        base = {
            "scope_id": SCOPE,
            "content_id": CONTENT,
            "revision": 1,
            "space_id": SPACE,
            "ordinal": 0,
            "policy_identity": "chunk.paragraph@1",
        }
        base.update(overrides)
        return keys.chunk_key(**base)

    assert key() == key()
    assert key(ordinal=1) != key()
    assert key(space_id=uuid.uuid4()) != key()
    assert key(policy_identity="chunk.paragraph@2") != key()
    assert key(revision=2) != key()
    assert key().version == 5


def test_distillation_keys_are_stable_per_transform_identity():
    first = keys.distillation_key(
        scope_id=SCOPE, content_id=CONTENT, revision=1, transform_identity="distill.extractive@1"
    )
    second = keys.distillation_key(
        scope_id=SCOPE, content_id=CONTENT, revision=1, transform_identity="distill.extractive@2"
    )
    assert first == keys.distillation_key(
        scope_id=SCOPE, content_id=CONTENT, revision=1, transform_identity="distill.extractive@1"
    )
    assert first != second


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


async def _stub_handler(context: AttemptContext, services) -> ExecutionReport:
    return ExecutionReport(outcome=Outcome.OK)


def test_registry_rejects_unknown_kinds_roles_and_conflicts():
    registry = JobRegistry()
    spec = registry.register_job_handler("graph.code.extract", "graph", _stub_handler)
    assert registry.handler_for("graph.code.extract") is spec
    assert registry.kinds_for_role("graph") == ("graph.code.extract",)
    assert registry.kinds_for_role("embed") == ()

    # Re-registering the identical handler is idempotent, not a conflict.
    assert registry.register_job_handler("graph.code.extract", "graph", _stub_handler) is spec

    with pytest.raises(RegistryConflict):
        registry.register_job_handler("graph.code.extract", "embed", _stub_handler)
    with pytest.raises(RegistryError):
        registry.register_job_handler("invented.kind", "graph", _stub_handler)
    with pytest.raises(RegistryError):
        registry.register_job_handler("graph.memory.extract", "processor", _stub_handler)
    with pytest.raises(RegistryError):
        registry.register_job_handler("graph.memory.extract", "graph", "not-callable")


def test_registry_covers_every_versioned_kind_and_builtin_roles():
    assert set(queue.CONTENT_PINNED_KINDS) <= set(JOB_KINDS)
    for role in WORKER_ROLES:
        assert role in WORKER_ROLES
    for role in ("core", "doc", "image", "audio", "video"):
        assert role in EXECUTOR_ROLES


def test_registry_module_loading_fails_closed():
    registry = JobRegistry()
    with pytest.raises(RegistryError):
        registry.load_modules(["cortex_v2.processing.does_not_exist"])
    assert registry.load_modules([]) == ()
    assert registry.load_modules(["  ", "cortex_v2.processing.formats"]) == (
        "cortex_v2.processing.formats",
    )


def test_builtin_handlers_register_for_their_roles():
    from cortex_v2.processing import handlers

    registry = JobRegistry()
    handlers.register_builtin_handlers(registry)
    assert registry.kinds_for_role("doc") == ("doc.extract",)
    assert set(registry.kinds_for_role("embed")) == {
        "embed.chunks",
        "transform.distill",
        "transform.compact",
    }
    for kind in registry.kinds():
        spec = registry.handler_for(kind)
        assert spec is not None
        assert spec.publisher is not None, kind
        assert spec.summary


def test_registration_is_idempotent_across_calls():
    from cortex_v2.processing import handlers

    registry = JobRegistry()
    handlers.register_builtin_handlers(registry)
    handlers.register_builtin_handlers(registry)
    assert len(registry.kinds()) == 4


def test_process_registry_exposes_the_module_level_helper():
    from cortex_v2.processing import registry as registry_module

    spec = registry_module.register_job_handler(
        "graph.memory.extract", "graph", _stub_handler, summary="test"
    )
    assert JOB_REGISTRY.handler_for("graph.memory.extract") is spec


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_signature_outranks_a_lying_media_type():
    detection = formats.detect_format(
        media_type="text/plain", filename="notes.txt", head=b"%PDF-1.7\n"
    )
    assert detection.format_id == "pdf"
    assert detection.detected_by == "signature"


def test_media_type_outranks_extension_and_content():
    assert formats.detect_format(media_type="text/csv", filename="data.txt").format_id == "csv"
    assert formats.detect_format(filename="report.md").format_id == "markdown"
    assert formats.detect_format(filename="archive.tar.gz").format_id == "archive"


def test_container_and_brand_signatures_resolve():
    assert formats.detect_format(head=b"PK\x03\x04rest").format_id == "zip_container"
    assert formats.spec_for("zip_container").refine is True
    assert formats.detect_format(head=b"\x00\x00\x00\x18ftypmp42").format_id == "video"
    assert formats.detect_format(head=b"RIFF\x00\x00\x00\x00WAVEfmt ").format_id == "audio"
    assert formats.detect_format(head=b"RIFF\x00\x00\x00\x00WEBPVP8 ").format_id == "image"
    assert formats.detect_format(head=b"\x89PNG\r\n\x1a\n").format_id == "image"
    assert formats.detect_format(head=b"{\\rtf1\\ansi").format_id == "rtf"
    assert formats.detect_format(head=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1").format_id == "ole_legacy"


def test_binary_without_a_signature_is_never_laundered_into_text():
    detection = formats.detect_format(head=b"\x00\x01\x02\x03binary")
    assert detection.format_id == "unknown_binary"
    assert formats.spec_for("unknown_binary").status == "unsupported"


def test_text_content_is_sniffed_when_nothing_else_is_known():
    assert formats.detect_format(body_text='{"a": 1}').format_id == "json"
    assert formats.detect_format(body_text="From mboxrd@z Thu Sep 25 00:00:00 2026\nSubject: x\n").format_id == "mbox"
    assert formats.detect_format(body_text="From: a@b\nSubject: hi\n\nbody").format_id == "eml"
    assert formats.detect_format(body_text="# Title\n\nsome text").format_id == "markdown"
    assert formats.detect_format(body_text="just prose without markers").format_id == "unknown_text"
    assert formats.detect_format(body_text="def f():\n    import os\n").format_id == "source_code"


def test_required_role_is_the_narrowest_role_that_can_serve_the_format():
    assert formats.required_role_for("text") == "core"
    assert formats.required_role_for("pdf") == "doc"
    assert formats.required_role_for("docx") == "doc"
    assert formats.required_role_for("image") == "image"
    assert formats.required_role_for("audio") == "audio"
    assert formats.required_role_for("video") == "video"
    # An unsupported format still names a role so the job is visibly blocked.
    assert formats.required_role_for("ole_legacy") == "core"
    assert formats.required_role_for(None) == "core"


def test_format_table_is_consistent_and_discoverable():
    ids = [spec.format_id for spec in formats.FORMAT_TABLE]
    assert len(ids) == len(set(ids))
    table = formats.discovery_table()
    assert len(table["formats"]) == len(formats.FORMAT_TABLE)
    assert table["detection_precedence"] == ["signature", "media_type", "extension", "content"]
    for spec in formats.FORMAT_TABLE:
        assert set(spec.roles) <= set(EXECUTOR_ROLES)
        if spec.status == "tested":
            assert spec.parser_id, spec.format_id
        if spec.dependencies:
            assert spec.status == "optional_dependency", spec.format_id
        if spec.status == "routed_to_media_role":
            assert spec.roles and spec.roles[0] in ("image", "audio", "video")
    documented = {spec.format_id for spec in formats.FORMAT_TABLE}
    for required in (
        "text", "markdown", "html", "pdf", "csv", "tsv", "json", "jsonl", "yaml",
        "xml", "docx", "pptx", "xlsx", "odt", "odp", "ods", "rtf", "epub", "eml",
        "mbox", "source_code",
    ):
        assert required in documented


def test_dispatch_reports_typed_outcomes_instead_of_raising():
    unsupported = parsers.dispatch(
        source_ref("x", media_type="application/vnd.ms-word", filename="legacy.doc"),
    )
    assert unsupported.outcome is Outcome.UNSUPPORTED_FORMAT
    assert unsupported.format_id == "ole_legacy"

    image = parsers.dispatch(source_ref("", media_type="image/png", filename="a.png"))
    assert image.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
    assert image.required_role == "image"

    bad_limits = parsers.dispatch(
        source_ref("hello"), limits=ParserLimits(max_input_chars=0)
    )
    assert bad_limits.outcome is Outcome.INPUT_REJECTED


def test_parser_registry_reports_its_own_state():
    report = parsers.registry_report()
    assert set(report) == {"available", "dependencies", "adapters"}
    assert parsers.parser_for(None) is None
    assert parsers.parser_for("not-a-format") is None
    assert isinstance(report["adapters"], list) and report["adapters"]


# ---------------------------------------------------------------------------
# Blob port confinement
# ---------------------------------------------------------------------------


def run(coroutine):
    return asyncio.run(coroutine)


def test_blob_resolver_confines_reads_to_its_root(tmp_path):
    root = tmp_path / "blobs"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "doc.txt").write_bytes(b"original bytes")
    outside = tmp_path / "secret.txt"
    outside.write_text("ambient credential")
    os.symlink(outside, root / "nested" / "escape.txt")
    resolver = FilesystemBlobResolver(root)

    ok = run(resolver.read("nested/doc.txt"))
    assert ok.ok and ok.data == b"original bytes"

    for location in ("../secret.txt", "/etc/passwd", "nested/../../secret.txt", "a\x00b"):
        refused = run(resolver.read(location))
        assert refused.outcome is Outcome.INPUT_REJECTED, location

    escaped = run(resolver.read("nested/escape.txt"))
    assert escaped.outcome is Outcome.INPUT_REJECTED

    missing = run(resolver.read("nested/gone.txt"))
    assert missing.outcome is Outcome.SOURCE_BYTES_UNAVAILABLE


def test_blob_resolver_verifies_the_recorded_hash_and_size_bound(tmp_path):
    import hashlib

    root = tmp_path / "blobs"
    root.mkdir()
    (root / "a.bin").write_bytes(b"abc")
    (root / "big.bin").write_bytes(b"x" * 10)
    resolver = FilesystemBlobResolver(root)

    good = run(resolver.read("a.bin", expected_sha256=hashlib.sha256(b"abc").digest()))
    assert good.ok
    bad = run(resolver.read("a.bin", expected_sha256=hashlib.sha256(b"other").digest()))
    assert bad.outcome is Outcome.INPUT_CORRUPT
    too_big = run(resolver.read("big.bin", max_bytes=4))
    assert too_big.outcome is Outcome.QUOTA_EXCEEDED


def test_unconfigured_blob_store_is_explicitly_unavailable():
    resolver = resolver_from_root(None)
    assert isinstance(resolver, NullBlobResolver)
    result = run(resolver.read("anything"))
    assert result.outcome is Outcome.SOURCE_BYTES_UNAVAILABLE
    assert "not dereferenced" in result.detail


def test_expected_digest_only_accepts_lowercase_sha256_hex():
    assert expected_digest({"bytes_sha256": "a" * 64}) == b"\xaa" * 32
    assert expected_digest({"bytes_sha256": "nope"}) is None
    assert expected_digest({}) is None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


def test_chunking_policy_input_enforces_its_own_geometry():
    policy = ChunkingPolicyInput(
        policy_id="chunk.paragraph",
        version=1,
        kind="structural",
        target_chars=1200,
        max_chars=2000,
        overlap_chars=0,
    )
    assert policy.model_dump()["policy_id"] == "chunk.paragraph"
    with pytest.raises(ValueError):
        ChunkingPolicyInput(
            policy_id="p", version=1, kind="structural",
            target_chars=2000, max_chars=1200, overlap_chars=0,
        )
    with pytest.raises(ValueError):
        ChunkingPolicyInput(
            policy_id="p", version=1, kind="structural",
            target_chars=1200, max_chars=2000, overlap_chars=1200,
        )
    with pytest.raises(ValueError):
        ChunkingPolicyInput(
            policy_id="p", version=1, kind="vibes",
            target_chars=1200, max_chars=2000, overlap_chars=0,
        )
    with pytest.raises(ValueError):
        ChunkingPolicyInput(
            policy_id="p", version=1, kind="structural",
            target_chars=1200, max_chars=2000, overlap_chars=0, unexpected=1,
        )


def test_space_request_carries_a_versioned_chunking_block():
    request = CreateEmbeddingSpaceRequest(
        space_name="hash-8",
        provider="hash-local",
        model_id="hash-local-1",
        dimensions=8,
        normalization="l2",
        metric="cosine",
        chunking=ChunkingPolicyInput(
            policy_id="chunk.paragraph", version=1, kind="structural",
            target_chars=1200, max_chars=2000, overlap_chars=0,
        ),
    )
    block = request.chunking_block()
    assert block["schema_version"] == 1
    assert block["policy_id"] == "chunk.paragraph"
    with pytest.raises(ValueError):
        CreateEmbeddingSpaceRequest(
            space_name="x" * 97,
            provider="hash-local",
            model_id="m",
            dimensions=8,
            normalization="l2",
            metric="cosine",
            chunking=ChunkingPolicyInput(
                policy_id="p", version=1, kind="structural",
                target_chars=1200, max_chars=2000, overlap_chars=0,
            ),
        )
    with pytest.raises(ValueError):
        CreateEmbeddingSpaceRequest(
            space_name="s",
            provider="hash-local",
            model_id="m",
            dimensions=0,
            normalization="l2",
            metric="cosine",
            chunking=ChunkingPolicyInput(
                policy_id="p", version=1, kind="structural",
                target_chars=1200, max_chars=2000, overlap_chars=0,
            ),
        )


def test_backfill_defaults_are_bounded_and_resumable():
    request = BackfillRequest(profile_id=PROFILE, space_id=SPACE)
    assert request.generation == "active"
    assert request.stage == "auto"
    assert request.then_embed is True
    assert 1 <= request.limit <= 200
    with pytest.raises(ValueError):
        BackfillRequest(profile_id=PROFILE, space_id=SPACE, limit=5000)


def test_list_jobs_request_rejects_unknown_filters():
    assert ListJobsRequest().limit == 25
    with pytest.raises(ValueError):
        ListJobsRequest(status="exploded")
    with pytest.raises(ValueError):
        ListJobsRequest(unknown_filter=1)


# ---------------------------------------------------------------------------
# Published operation contract
# ---------------------------------------------------------------------------


def test_operations_satisfy_the_integration_contract():
    seen = set()
    for operation in OPERATIONS:
        assert set(operation) >= {
            "operation_id",
            "method",
            "path",
            "kind",
            "request_model",
            "handler",
            "summary",
            "usage",
        }, operation.get("operation_id")
        assert operation["operation_id"] not in seen
        seen.add(operation["operation_id"])
        assert operation["operation_id"].startswith("processing.")
        assert operation["method"] in ("GET", "POST")
        assert operation["path"].startswith("/v1/")
        assert operation["kind"] in ("scoped_write", "scoped_read")
        assert callable(operation["handler"])
        assert operation["summary"]
        assert isinstance(operation["usage"], dict)
        assert operation["usage"]["use_when"]
        assert operation["usage"]["avoid_when"]
        model = operation["request_model"]
        assert model is None or (
            isinstance(model, type) and issubclass(model, BaseModel)
        )
        parameters = list(inspect.signature(operation["handler"]).parameters)
        if operation["kind"] == "scoped_write":
            assert parameters == [
                "connection",
                "context",
                "idempotency_key",
                "payload",
                "path_params",
            ], operation["operation_id"]
            assert operation["method"] == "POST"
            assert model is not None, operation["operation_id"]
            assert operation["usage"]["receipt"]
            assert operation["usage"]["idempotency"]
        else:
            assert parameters == ["connection", "context", "payload", "path_params"], (
                operation["operation_id"]
            )


def test_operations_cover_the_required_surface():
    ids = {operation["operation_id"] for operation in OPERATIONS}
    assert ids == {
        "processing.backfill",
        "processing.jobs.cancel",
        "processing.jobs.status",
        "processing.jobs.list",
        "processing.coverage",
        "processing.embedding-spaces.list",
        "processing.embedding-spaces.create",
        "processing.embedding-spaces.activate-generation",
        "processing.profiles.list",
        "processing.doc.formats",
    }


def test_path_parameters_match_the_declared_paths():
    for operation in OPERATIONS:
        declared = set(re.findall(r"\{([a-z_]+)\}", operation["path"]))
        source = inspect.getsource(operation["handler"])
        for name in declared:
            assert f'"{name}"' in source, (operation["operation_id"], name)


def test_chunking_policy_contract_shape():
    policy = ChunkingPolicy(
        policy_id="chunk.paragraph",
        version=1,
        kind="structural",
        target_chars=1200,
        max_chars=2000,
        overlap_chars=0,
    )
    assert policy.identity == "chunk.paragraph@1"


# ---------------------------------------------------------------------------
# Worker role capabilities and the claim predicate they feed
# ---------------------------------------------------------------------------


async def _noop_publisher(connection, context, report) -> None:
    return None


def graph_registry() -> JobRegistry:
    from cortex_v2.processing.handlers import register_builtin_handlers

    registry = JobRegistry()
    register_builtin_handlers(registry)

    async def graph_handler(context, services):
        return ExecutionReport(outcome=Outcome.OK)

    for kind in ("graph.code.extract", "graph.memory.extract"):
        registry.register_job_handler(
            kind, "graph", graph_handler, publisher=_noop_publisher
        )
    return registry


def test_only_the_graph_worker_claims_graph_kinds():
    """The claim filter is required_role = ANY(capabilities) AND kind = ANY(role kinds).

    Retrieval pins ``required_role='graph'`` on its intents, so a worker that
    advertises only ``core`` could never claim them — this pins the mapping.
    """
    from cortex_v2.processing import runtime

    assert runtime.executor_capabilities("graph") == ("core", "graph")
    assert runtime.executor_capabilities("doc") == ("core", "doc")
    assert runtime.executor_capabilities("embed") == ("core",)
    assert runtime.executor_capabilities("invented") == ()

    registry = graph_registry()
    for role in ("doc", "embed", "graph"):
        kinds = registry.kinds_for_role(role)
        capabilities = runtime.executor_capabilities(role)
        for kind in ("graph.code.extract", "graph.memory.extract"):
            claimable = kind in kinds and "graph" in capabilities
            assert claimable is (role == "graph"), (role, kind)
        assert ("embed.chunks" in kinds) is (role == "embed")
        assert ("doc.extract" in kinds) is (role == "doc")


def test_graph_intents_resolve_to_the_canonical_graph_executor_role():
    intent = JobIntent(
        job_kind="graph.code.extract",
        payload={"pin": {"repository_key": "helix", "commit_sha": "cafebabe"}},
    )
    intent.validate()
    assert queue.resolve_required_role(intent, None) == "graph"
    # An explicit graph role is accepted; any other role is refused at the
    # boundary as well as by the database check.
    JobIntent(
        job_kind="graph.memory.extract",
        required_role="graph",
        payload={"pin": {"commit_sha": "abc"}},
    ).validate()
    with pytest.raises(ApiProblem):
        JobIntent(
            job_kind="graph.memory.extract",
            required_role="core",
            payload={"pin": {"commit_sha": "abc"}},
        ).validate()


def test_configure_worker_is_the_public_registration_hook():
    from cortex_v2.processing.runtime import configure_worker

    registry = configure_worker(JobRegistry())
    assert registry.kinds_for_role("doc") == ("doc.extract",)
    assert set(registry.kinds_for_role("embed")) == {
        "embed.chunks",
        "transform.distill",
        "transform.compact",
    }
    # Idempotent for identical handlers, fail-closed for a missing module.
    assert configure_worker(registry) is registry
    with pytest.raises(RegistryError):
        configure_worker(registry, handler_modules=["cortex_v2.processing.nope"])


def test_worker_dsn_requires_a_named_instance_and_pins_its_host(monkeypatch):
    """A worker must never start against an unnamed or foreign lane.

    W1 and other v2 lanes share the runtime role and database name, so the
    instance profile's database host is the only thing that separates them.
    """
    from cortex_v2.config import ConfigurationError
    from cortex_v2.processing import runtime

    class _Worker:
        database_url_file = "CORTEX_V2_DATABASE_URL_FILE"
        principal_id_file = "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE"

    class _Settings:
        worker = _Worker()

    def dsn(value: str) -> None:
        monkeypatch.setattr(
            runtime,
            "read_secret_path",
            lambda name, minimum_bytes=1: value.encode(),
        )

    # Absent instance: fail closed exactly like the API's active_profile().
    monkeypatch.delenv("CORTEX_V2_SANDBOX_INSTANCE", raising=False)
    dsn("postgresql://cortex_v2_app@w1-db:5432/cortex_v2")
    with pytest.raises(ConfigurationError):
        runtime.resolve_database_url(_Settings())

    # Unknown instance: fail closed, no unpinned fallback.
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", "some-other-lane")
    with pytest.raises(ConfigurationError):
        runtime.resolve_database_url(_Settings())

    # Named W1 candidate: only its own database host is accepted.
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", "cortex-v2-w1-candidate")
    w1 = "postgresql://cortex_v2_app@w1-db:5432/cortex_v2"
    dsn(w1)
    assert runtime.resolve_database_url(_Settings()) == w1
    for bad in (
        "postgresql://cortex_v2_app@db:5432/cortex_v2",           # sandbox lane host
        "postgresql://cortex_v2_app@kai-test-db:5432/cortex_v2",  # another lane
        "postgresql://cortex_v2_migrator@w1-db:5432/cortex_v2",   # privileged role
        "postgresql://cortex_v2_app@w1-db:5432/postgres",         # wrong database
        "mysql://cortex_v2_app@w1-db:5432/cortex_v2",             # wrong scheme
    ):
        dsn(bad)
        with pytest.raises(ConfigurationError):
            runtime.resolve_database_url(_Settings())

    # The pin follows the profile: the sandbox lane accepts only its own host.
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", "cortex-v2-v0-02-001-sandbox")
    sandbox = "postgresql://cortex_v2_app@db:5432/cortex_v2"
    dsn(sandbox)
    assert runtime.resolve_database_url(_Settings()) == sandbox
    dsn(w1)
    with pytest.raises(ConfigurationError):
        runtime.resolve_database_url(_Settings())


def test_worker_identity_reads_secret_references_by_env_name(monkeypatch):
    """Every *_FILE setting is a NAME whose value is a path: no double lookup.

    Regression: passing the resolved path into read_secret_path made launched
    workers exit 2 with 'required secret-file setting is missing: /run/secrets/…'.
    """
    import asyncio
    from types import SimpleNamespace

    from cortex_v2.processing import runtime
    from cortex_v2.store import Principal

    seen: list[tuple[str, int]] = []
    secrets = {
        "CORTEX_V2_WORKER_TOKEN_FILE": b"worker-token",
        "CORTEX_V2_TOKEN_PEPPER_FILE": b"ab" * 32,
        "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE": b"11111111-1111-4111-8111-111111111111",
        "CORTEX_V2_WORKER_INSTALLATION_ID_FILE": b"22222222-2222-4222-8222-222222222222",
    }

    def fake_read(name, minimum_bytes=1):
        seen.append((name, minimum_bytes))
        if name not in secrets:
            raise RuntimeError(f"unexpected secret reference {name!r}")
        return secrets[name]

    principal = Principal(
        uuid.UUID("33333333-3333-4333-8333-333333333333"),
        uuid.UUID("44444444-4444-4444-8444-444444444444"),
    )

    async def fake_authenticate(connection, digest):
        return principal

    class _Connection:
        async def execute(self, sql, *args):
            return None

        async def fetchval(self, sql, *args):
            return True

    monkeypatch.setattr(runtime, "read_secret_path", fake_read)
    monkeypatch.setattr(runtime, "authenticate", fake_authenticate)
    settings = SimpleNamespace(
        worker=SimpleNamespace(
            principal_id_file="CORTEX_V2_WORKER_PRINCIPAL_ID_FILE",
            database_url_file="CORTEX_V2_DATABASE_URL_FILE",
        )
    )
    worker = runtime.Worker(settings=settings, role="embed", worker_id="it-1")

    # Credential path: the token and pepper references are read by NAME.
    monkeypatch.setenv("CORTEX_V2_WORKER_TOKEN_FILE", "/run/secrets/kai-worker-token")
    monkeypatch.setenv("CORTEX_V2_TOKEN_PEPPER_FILE", "/run/secrets/kai-pepper")
    seen.clear()
    identity = asyncio.run(worker._resolve_identity(_Connection()))
    assert (identity.principal_id, identity.installation_id) == (
        principal.principal_id,
        principal.installation_id,
    )
    assert seen == [
        ("CORTEX_V2_WORKER_TOKEN_FILE", 1),
        ("CORTEX_V2_TOKEN_PEPPER_FILE", 64),
    ]

    # Principal-id path: both references are read by NAME as well.
    monkeypatch.delenv("CORTEX_V2_WORKER_TOKEN_FILE")
    monkeypatch.delenv("CORTEX_V2_TOKEN_PEPPER_FILE")
    monkeypatch.setenv(
        "CORTEX_V2_WORKER_INSTALLATION_ID_FILE", "/run/secrets/kai-installation"
    )
    seen.clear()
    identity = asyncio.run(worker._resolve_identity(_Connection()))
    assert identity.installation_id == uuid.UUID(
        "22222222-2222-4222-8222-222222222222"
    )
    assert seen == [
        ("CORTEX_V2_WORKER_PRINCIPAL_ID_FILE", 1),
        ("CORTEX_V2_WORKER_INSTALLATION_ID_FILE", 1),
    ]

    # Neither configured: a precise configuration error, not a secret lookup.
    monkeypatch.delenv("CORTEX_V2_WORKER_INSTALLATION_ID_FILE")
    from cortex_v2.config import ConfigurationError

    with pytest.raises(ConfigurationError):
        asyncio.run(worker._resolve_identity(_Connection()))
