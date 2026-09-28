from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from cortex_v2.ops.backup import BACKUP_TABLES, fold_row_digest
from cortex_v2.ops.models import (
    RepairRequest,
    RetentionArchiveRequest,
    RetentionEnactRequest,
)
from cortex_v2.ops.operations import OPERATIONS

REQUIRED_KEYS = {
    "operation_id",
    "method",
    "path",
    "kind",
    "request_model",
    "handler",
    "summary",
    "usage",
}


def test_every_operation_declares_the_full_contract() -> None:
    assert OPERATIONS
    ids = [operation["operation_id"] for operation in OPERATIONS]
    assert len(set(ids)) == len(ids)
    for operation in OPERATIONS:
        missing = REQUIRED_KEYS - operation.keys()
        assert not missing, (operation.get("operation_id"), missing)
        assert operation["kind"] in ("scoped_write", "scoped_read")
        assert operation["method"] in ("GET", "POST")
        assert operation["path"].startswith("/v1/")
        assert operation["summary"] and operation["usage"]
        assert inspect.iscoroutinefunction(operation["handler"])


def test_handler_signatures_match_their_kind() -> None:
    for operation in OPERATIONS:
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
        else:
            assert parameters == [
                "connection",
                "context",
                "payload",
                "path_params",
            ], operation["operation_id"]


def test_write_operations_carry_request_models_reads_do_not() -> None:
    by_id = {operation["operation_id"]: operation for operation in OPERATIONS}
    for operation_id in (
        "ops.retention_enact",
        "ops.retention_archive",
        "ops.repair",
    ):
        assert by_id[operation_id]["request_model"] is not None
    for operation_id in ("ops.backup_create", "ops.doctor", "ops.backup_verify"):
        assert by_id[operation_id]["request_model"] is None


def test_retention_models_reject_invalid_input() -> None:
    with pytest.raises(ValidationError):
        RetentionEnactRequest(
            content_class="knowledge",
            min_age_days=-1,
            action="archive",
            expected_revision=0,
        )
    with pytest.raises(ValidationError):
        RetentionEnactRequest(
            content_class="mood",
            min_age_days=1,
            action="archive",
            expected_revision=0,
        )
    with pytest.raises(ValidationError):
        RetentionEnactRequest(
            content_class="knowledge",
            min_age_days=1,
            action="delete",
            expected_revision=0,
        )
    with pytest.raises(ValidationError):
        RetentionEnactRequest(
            content_class="knowledge",
            min_age_days=1,
            action="archive",
            expected_revision=0,
            extra_field="nope",
        )
    valid = RetentionEnactRequest(
        content_class="*", min_age_days=0, action="retain", expected_revision=3
    )
    assert valid.min_age_days == 0


def test_archive_and_repair_models() -> None:
    assert RetentionArchiveRequest().dry_run is False
    assert RetentionArchiveRequest(content_class="diary", dry_run=True).content_class == "diary"
    with pytest.raises(ValidationError):
        RepairRequest(repair_kind="drop_tables", target_reference="x")
    with pytest.raises(ValidationError):
        RepairRequest(repair_kind="retention_restore", target_reference="")
    assert (
        RepairRequest(
            repair_kind="retention_restore", target_reference="abc"
        ).repair_kind
        == "retention_restore"
    )


def test_backup_and_repair_targets_are_fixed_allowlists() -> None:
    assert len(set(BACKUP_TABLES)) == len(BACKUP_TABLES)
    assert all(table.startswith(("cortex_core.", "cortex_auth.")) for table in BACKUP_TABLES)
    assert "cortex_core.backup_manifests" not in BACKUP_TABLES
    assert "cortex_core.command_receipts" not in BACKUP_TABLES


def test_backup_fold_digest_is_bounded_deterministic_order_sensitive() -> None:
    import hashlib

    count, digest = fold_row_digest([])
    assert (count, digest) == (0, hashlib.sha256(b"").hexdigest())
    count_ab, digest_ab = fold_row_digest(["aa", "bb"])
    count_ba, digest_ba = fold_row_digest(["bb", "aa"])
    assert (count_ab, count_ba) == (2, 2)
    assert digest_ab != digest_ba
    expected = hashlib.sha256()
    expected.update(b"aa")
    expected.update(b"bb")
    assert digest_ab == expected.hexdigest()
    assert fold_row_digest(["aa", "bb"]) == (count_ab, digest_ab)


def test_repair_kinds_exclude_feed_owned_outbox_redelivery() -> None:
    with pytest.raises(ValidationError):
        RepairRequest(repair_kind="content_outbox_redeliver", target_reference="x")
    with pytest.raises(ValidationError):
        RepairRequest(repair_kind="memory_outbox_redeliver", target_reference="x")
    assert (
        RepairRequest(
            repair_kind="retention_restore", target_reference="x"
        ).repair_kind
        == "retention_restore"
    )
