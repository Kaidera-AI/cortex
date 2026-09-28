"""W4 operations registered through the generic operation contract."""

from __future__ import annotations

from typing import Any

from .backup import create_backup, verify_backup
from .doctor import run_doctor
from .metrics import run_metrics
from .models import RepairRequest, RetentionArchiveRequest, RetentionEnactRequest
from .repair import run_repair
from .retention import archive_retention, enact_retention, retention_status
from .status import run_migration_status, run_release_status

OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "ops.doctor",
        "method": "GET",
        "path": "/v1/ops/doctor",
        "kind": "scoped_read",
        "request_model": None,
        "handler": run_doctor,
        "summary": (
            "Effect-based doctor: configured versus applied runtime role, forced "
            "RLS, definer authentication, migration ledger and outbox health."
        ),
        "usage": (
            "Call with a selected scope. Inspect checks[].status; degraded checks "
            "name the missing applied effect. Coverage is the caller's RLS view."
        ),
    },
    {
        "operation_id": "ops.metrics",
        "method": "GET",
        "path": "/v1/ops/metrics",
        "kind": "scoped_read",
        "request_model": None,
        "handler": run_metrics,
        "summary": (
            "Operational counters for commands, content, outbox, retention and "
            "repairs without content or secret leakage."
        ),
        "usage": (
            "Call with a selected scope; unavailable[] lists measurement planes "
            "this release does not provide."
        ),
    },
    {
        "operation_id": "ops.migration_status",
        "method": "GET",
        "path": "/v1/ops/migration-status",
        "kind": "scoped_read",
        "request_model": None,
        "handler": run_migration_status,
        "summary": (
            "Applied-versus-expected migration ledger with per-file state: "
            "applied, missing, checksum_mismatch or checksum_unverified."
        ),
        "usage": "Compare expected_order with migrations[].state before upgrades.",
    },
    {
        "operation_id": "ops.release_status",
        "method": "GET",
        "path": "/v1/ops/release-status",
        "kind": "scoped_read",
        "request_model": None,
        "handler": run_release_status,
        "summary": (
            "Release identity: product/API/contract versions, instance, "
            "deployment class and schema range."
        ),
        "usage": "Read before staging clients; release_class states the lane.",
    },
    {
        "operation_id": "ops.retention_status",
        "method": "GET",
        "path": "/v1/ops/retention-status",
        "kind": "scoped_read",
        "request_model": None,
        "handler": retention_status,
        "summary": "Active retention policy revision per content class.",
        "usage": "Read the current revision before enacting a policy change.",
    },
    {
        "operation_id": "ops.retention_enact",
        "method": "POST",
        "path": "/v1/ops/retention-policies",
        "kind": "scoped_write",
        "request_model": RetentionEnactRequest,
        "handler": enact_retention,
        "summary": (
            "Owner-enacted append-only retention policy revision with optimistic "
            "expected_revision."
        ),
        "usage": (
            "Send content_class, min_age_days, action and expected_revision; a "
            "moved revision returns policy_revision_conflict."
        ),
    },
    {
        "operation_id": "ops.retention_archive",
        "method": "POST",
        "path": "/v1/ops/retention:archive",
        "kind": "scoped_write",
        "request_model": RetentionArchiveRequest,
        "handler": archive_retention,
        "summary": (
            "Ledgered in-place archiving of policy-eligible content; originals "
            "are never rewritten. dry_run previews the batch."
        ),
        "usage": (
            "Optionally filter content_class; entries carry hashes and entry ids "
            "for typed restore repairs."
        ),
    },
    {
        "operation_id": "ops.repair",
        "method": "POST",
        "path": "/v1/ops/repairs",
        "kind": "scoped_write",
        "request_model": RepairRequest,
        "handler": run_repair,
        "summary": (
            "Typed retention-restore repair with preconditions and a durable "
            "ledger outcome; outbox redelivery belongs to the feed contract."
        ),
        "usage": (
            "Send repair_kind and a UUID target_reference; outcome is applied, "
            "precondition_failed or not_found — never silent success."
        ),
    },
    {
        "operation_id": "ops.backup_create",
        "method": "POST",
        "path": "/v1/ops/backups",
        "kind": "scoped_write",
        "request_model": None,
        "handler": create_backup,
        "summary": (
            "Creates a lossless-verification manifest: schema ledger, scope "
            "coverage and per-table row counts plus ordered row digests."
        ),
        "usage": (
            "Idempotent by key; the manifest covers the caller's RLS view and "
            "records that honestly."
        ),
    },
    {
        "operation_id": "ops.backup_verify",
        "method": "GET",
        "path": "/v1/ops/backups/{manifest_id}/verify",
        "kind": "scoped_read",
        "request_model": None,
        "handler": verify_backup,
        "summary": (
            "Recomputes manifest digests: verified, verified_coverage_differs "
            "or drifted with the exact drifted tables."
        ),
        "usage": "Run after restores or before/after bulk operations.",
    },
]
