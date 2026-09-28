"""DB-backed integration suite for the Verification module (R19).

Proves the honesty rules end to end: evidence-backed verdicts with exact
source/revision citations and readback evidence, explicit UNVERIFIABLE with
a reason, producer independence (no result laundering), fail-closed citation
validation, and the published read model that coordination's work-product
receipt view consumes.

Gated on the same environment as the coordination/feed integration suites.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DATABASE_URL = os.environ.get("CORTEX_V2_TEST_DATABASE_URL")
MIGRATOR_URL = os.environ.get("CORTEX_V2_TEST_MIGRATOR_DATABASE_URL")

if not DATABASE_URL or not MIGRATOR_URL:
    pytest.skip(
        "verification integration suite requires a candidate database "
        "(CORTEX_V2_TEST_DATABASE_URL / CORTEX_V2_TEST_MIGRATOR_DATABASE_URL)",
        allow_module_level=True,
    )

from test_coordination_integration import (  # noqa: E402
    FIXTURE,
    _command,
    _create_handoff,
    _create_request,
    as_owner,
    as_reviewer,
    as_worker_a,
)

from cortex_v2 import content as content_uc  # noqa: E402
from cortex_v2.coordination import handoffs as handoff_uc  # noqa: E402
from cortex_v2.coordination import work_products as work_product_uc  # noqa: E402
from cortex_v2.coordination.models import (  # noqa: E402
    ClaimHandoffRequest,
    ReturnHandoffRequest,
    WorkProductReference,
)
from cortex_v2.models import CreateContentRequest  # noqa: E402
from cortex_v2.store import ApiProblem  # noqa: E402
from cortex_v2.verification import records as verification_uc  # noqa: E402
from cortex_v2.verification.models import (  # noqa: E402
    Citation,
    ReadbackItem,
    RecordVerificationRequest,
)


def _code(excinfo) -> str:
    return excinfo.value.code


def _make_receipt() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (handoff_id, content_id, receipt_id) for a fresh return."""

    async def flow() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        _s, content_receipt, _ = await as_worker_a(
            lambda conn, ctx: content_uc.create_content(
                conn, ctx,
                CreateContentRequest(
                    content_class="work_product",
                    payload={
                        "kind": "report",
                        "summary": "verification subject",
                        "references": [],
                    },
                    body="work product body",
                ),
                f"k-{uuid.uuid4()}",
            )
        )
        content_id = uuid.UUID(content_receipt["content_id"])
        _s, created, _ = await as_worker_a(
            lambda conn, ctx: handoff_uc.create_handoff(
                conn, ctx,
                _create_request(title="verification subject handoff"),
                f"k-{uuid.uuid4()}",
            )
        )
        handoff_id = uuid.UUID(created["handoff_id"])
        await as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(),
                f"k-{uuid.uuid4()}",
            )
        )
        _s, returned, _ = await as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(
                    expected_claim_generation=1,
                    summary="with product",
                    work_products=[
                        WorkProductReference(content_id=content_id)
                    ],
                ),
                f"k-{uuid.uuid4()}",
            )
        )
        receipt_id = uuid.UUID(
            returned["work_product_receipts"][0]["receipt_id"]
        )
        return handoff_id, content_id, receipt_id

    return asyncio.run(flow())


def test_bare_assertion_cannot_be_recorded_as_verified() -> None:
    with pytest.raises(ApiProblem) as failure:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="claim",
                        claim="the migration succeeded",
                        verdict="verified",
                        method="manual_review",
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(failure) == "verified_requires_citations"


def test_unverifiable_requires_explicit_reason_and_is_recordable() -> None:
    with pytest.raises(ApiProblem) as missing_reason:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="claim",
                        claim="tests passed somewhere",
                        verdict="unverifiable",
                        method="manual_review",
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(missing_reason) == "unverifiable_requires_reason"

    _status, receipt, _ = asyncio.run(
        as_reviewer(
            lambda conn, ctx: verification_uc.record_verification(
                conn, ctx,
                RecordVerificationRequest(
                    subject_kind="claim",
                    claim="tests passed somewhere",
                    verdict="unverifiable",
                    method="manual_review",
                    unverifiable_reason="no test output was retained",
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert receipt["verdict"] == "unverifiable"


def test_producer_cannot_verify_own_receipt_but_reviewer_can() -> None:
    handoff_id, content_id, receipt_id = _make_receipt()

    # The producing worker cannot launder its own return into verified.
    with pytest.raises(ApiProblem) as laundering:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="work_product_receipt",
                        subject_receipt_id=receipt_id,
                        verdict="verified",
                        method="content_readback",
                        citations=[
                            {"content_id": str(content_id), "revision": 1}
                        ],
                        readback_evidence=[
                            {
                                "kind": "content_readback",
                                "reference": f"content:{content_id}:1",
                                "observed": "body matches",
                            }
                        ],
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(laundering) == "self_verification_denied"

    # An independent reviewer records a verified verdict with citations.
    _status, receipt, _ = asyncio.run(
        as_reviewer(
            lambda conn, ctx: verification_uc.record_verification(
                conn, ctx,
                RecordVerificationRequest(
                    subject_kind="work_product_receipt",
                    subject_receipt_id=receipt_id,
                    verdict="verified",
                    method="content_readback",
                    citations=[
                        {
                            "content_id": str(content_id),
                            "revision": 1,
                            "note": "pinned revision read back",
                        }
                    ],
                    readback_evidence=[
                        {
                            "kind": "content_readback",
                            "reference": f"content:{content_id}:1",
                            "observed": "hash equals the pinned receipt hash",
                        }
                    ],
                    tool_name="cortex-readback",
                    tool_version="0.02.001",
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert receipt["verdict"] == "verified"
    verification_id = uuid.UUID(receipt["verification_id"])

    # The coordination receipt view now references the independent record.
    view = asyncio.run(
        as_worker_a(
            lambda conn, ctx: work_product_uc.get_work_product_receipt(
                conn, ctx, receipt_id
            ),
            write=False,
        )
    )
    assert view["attestation"] == "self_reported"
    independent = view["independent_verification"]
    assert independent is not None
    assert independent["verification_id"] == str(verification_id)
    assert independent["verdict"] == "verified"

    record = asyncio.run(
        as_reviewer(
            lambda conn, ctx: verification_uc.get_verification(
                conn, ctx, verification_id
            ),
            write=False,
        )
    )
    assert record["citations"][0]["cited_content_id"] == str(content_id)
    assert record["citations"][0]["cited_revision"] == 1
    assert record["readback_evidence"][0]["kind"] == "content_readback"


def test_citations_must_name_readable_exact_revisions() -> None:
    with pytest.raises(ApiProblem) as ghost:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="claim",
                        claim="something happened",
                        verdict="contradicted",
                        method="content_readback",
                        citations=[
                            {"content_id": str(uuid.uuid4()), "revision": 1}
                        ],
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(ghost) == "citation_not_found"


def test_subject_binding_is_enforced() -> None:
    with pytest.raises(ApiProblem) as mismatch:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="work_product_receipt",
                        subject_receipt_id=uuid.uuid4(),
                        verdict="unverifiable",
                        method="manual_review",
                        unverifiable_reason="gone",
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(mismatch) == "receipt_not_found"


def test_subject_listing_is_newest_first() -> None:
    handoff_id, _content_id, _receipt_id = _make_receipt()
    for index in range(2):
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: verification_uc.record_verification(
                    conn, ctx,
                    RecordVerificationRequest(
                        subject_kind="handoff_return",
                        subject_handoff_id=handoff_id,
                        verdict="unverifiable",
                        method="manual_review",
                        unverifiable_reason=f"pass {index}",
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    listing = asyncio.run(
        as_reviewer(
            lambda conn, ctx: verification_uc.list_for_subject(
                conn, ctx,
                {
                    "subject_kind": "handoff_return",
                    "subject_handoff_id": str(handoff_id),
                },
            ),
            write=False,
        )
    )
    items = listing["items"]
    assert len(items) == 2
    reasons = [item["unverifiable_reason"] for item in items]
    assert reasons == ["pass 1", "pass 0"]
