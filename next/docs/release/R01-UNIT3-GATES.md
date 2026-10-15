# R01 Unit 3 source gate inventory

Authority: Kai handoff `05b05604-0c53-4a23-9d5c-9dc2a7cae893`, approving residual EB1/F1 source work after the E-B/F reprice. This document records source tests and their limits. It is not installed-owner, Gate B physical, native, signing, readiness, install or release acceptance.

## Accepted source composition and step-zero baseline

The one authorized feature merge is `a33c4376b5bbcbe1ca6eabea90fb440fa2535434` with ordered parents:

1. Accepted P2b seed `8584588f26634f0aa72ce37e48cfcdd130df094e`.
2. Named current main `e5accd99511566cc02640df57026f28eeb31cf1e`, including the U3 Linux repair and four bridge partitions.

The merge was clean. All 62 checked install/runtime/install-oracle/frozen-oracle paths retained the exact P2b blobs. The original baseline below passed on CPython 3.13.7 and 3.14.0 before EB1/F1 edits. Use the existing offline gate and workers; the bridge must retain its four sequential partitions, sole 180-second child cap, complete selection and original order. No new runner or CI configuration is introduced.

| Existing U3 source file under `next/tests/release/` | Original selected cases per Python |
|---|---:|
| `test_aggregate_assembly.py` | 84 |
| `test_aggregate_contract.py` | 96 |
| `test_aggregate_custody_rework.py` | 7 |
| `test_aggregate_members.py` | 57 |
| `test_aggregate_replay.py` | 23 |
| `test_ed25519_provider.py` | 32 |
| `test_ed25519_strict_encoding.py` | 32 |
| `test_manifest_attestation_shape.py` | 17 |
| `test_manifest_current_alignment.py` | 31 |
| `test_manifest_current_companions.py` | 7 |
| `test_manifest_trust.py` | 51 |
| `test_manifest_trust_bounds.py` | 17 |
| `test_manifest_trust_companions.py` | 14 |
| `test_manifest_trust_guard_order.py` | 12 |
| `test_manifest_trust_policy_guards.py` | 7 |
| `test_member_admission_rework.py` | 6 |
| `test_oracle_plan_corrections.py` | 22 |
| `test_original_member_bridge.py` | 41 |
| `test_owner_amendments.py` | 23 |
| `test_verifier_boundary.py` | 62 |
| `test_verifier_boundary_aliases.py` | 5 |

The baseline is **21 files / 646 selected cases per Python**. EB1 appends 11 methods to the existing `test_verifier_boundary.py`; it does not retire an original method or create a new test-file selection path. Final source receipts must separately name original coverage and the new tests; a planned count is not a run receipt.

| Preserved source cohort | Exact selected method count per Python | Limit |
|---|---:|---|
| Finn P2a/source baseline | 317 | Source DATA and inert owned temporary history. |
| Finn P2b continuation/part 1 | 33 | Applied lineage/original-request continuity. |
| Finn P2b part 2 | 28 | Conditional recovery/fresh child/uninstall source joins. |
| Cox recovery/finite-owner | 116 | Existing source originals and finite SQL recorder controls; no real SQL execution. |
| Cox blocked-driver | 43 | Existing real import-refusal witness, not a fake driver. |
| Frozen PG-B2 + P2a | 53 | Existing 25 frozen oracle methods + 28 source P2a methods. |

These cohorts overlap. Do not sum them into a unique-test or native-pass total. Their original IDs/fixtures/assertions are preserved. Original 30 F2 execution controls and other installed/native/PG-C/PG-F qualification gates remain explicitly held; no source success replaces missing original execution material.

## EB1: real R01/RC2 inside the existing owner

`OwnerSourceJoin` reuses the existing `StartupFixture`, unrelated RAM test signing material, held-original reader, canonical fixture encoder and `OwnerProposalOracle` data/record helpers. It derives fresh TEST-ONLY P1 references to the actual signed aggregate/runtime-lock fixture, including the request and the protected scope's release-binding reference chain. It never edits the frozen owner subjects.

The deciding path is the actual fixed RC2 port → R01 member backend → real cryptography and original-byte custody checks → existing owner proposal. No RC2 double, signature PASS stub, alternate verifier, public binder or production provider is used on that path. Test substitution supplies only the existing private startup/owner observation DATA. Physical protection, independently admitted roots/privileges and a verified image are not produced by fixture records.

| EB1 deciding group | Required source observation |
|---|---|
| Valid joined original | At least four real Ed25519 calls and two original transports; exact original release/request binding; two owner observations; only `PRECONSUME_DATA`, all authority/native/reservation/consume flags false. |
| Altered comment signature | `r01_other_refusal` / `aggregate_signature_invalid` before lifecycle binding or backend entry. |
| Unsigned aggregate | `r01_other_refusal` / `aggregate_unsigned`. |
| Substituted independent root bytes | `r01_other_refusal` / `aggregate_root_mismatch`. |
| Corrupted held runtime-lock original | `r01_other_refusal` / `member_digest_mismatch`. |
| Owner nonce drift after real audit | `nonce_replay`; the real R01 audit actually ran. |
| Owner deadline crossing after real audit | `reservation_expired`; a repeated audit cannot renew the original deadline. |
| Caller authority flag or cached read-only snapshot | `invalid_request`; client DATA never becomes owner authority. |
| Peer mismatch / absent owner binding | Refuse before R01/input access as specified; production defaults stay unbound. |

Tests-first commit `f4f1b03b346bf5f23137e42a0b9c0c97e5610810` measured the accepted **source-test composition** before wiring this join: 7 BODY failures and 4 passing controls per Python. The old owner oracle used its nonauthoritative RC2 double, so the new real-verification/original-transport and release-negative assertions failed. This is an integration-oracle coverage change, not a claimed product verifier defect or physical boundary fix. The deciding assertions remain frozen through the test-composition change.

## F1: returned original receipt consumption and retained bridge IDs

The existing `Bridge.installer` helper now independently consumes the actual returned original receipt with the same existing `verify_product_receipt` function after the producer's own validation. It binds the consumer to the original canonical request bytes, independently measured request reference and exact returned evidence. The assertions require:

- Two successful uses of the one verifier: producer validation and consumer readback of exactly the same original bytes/request.
- Exact `request.json` and `attempts.json` bytes/references, with the existing empty forbidden-attempts source ledger.
- No installed resources, journal, native/effect flag or readiness credit from the read-only plan.
- No receipt-consumer success for the original bad-signature refusal.

The same four existing manual/KOS-prerequisite cases across both backend targets exercise this join. Tests-first they give four BODY failures per Python because only producer validation occurred; adding the test-owned consumer composition satisfies the frozen assertions. No new test ID, product receipt format, parser, verifier or channel is introduced. All original bridge test method bodies and dynamic case-generation functions are unchanged.

The following original 41 IDs retain their original collection order. Partition assignment remains the existing stride `selected[index::4]`, so partition sizes remain **11 / 10 / 10 / 10**, with no omitted/duplicate case:

1. `next/tests/release/test_original_member_bridge.py::test_authority`
2. `next/tests/release/test_original_member_bridge.py::Bridge::test_EOF_rehash_detects_actual_original_bytes_with_blinded_stat_checks`
3. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_bad_signature_refuses_before_member_open`
4. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_finn_linux_amd64_kos_prerequisite`
5. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_finn_linux_amd64_manual`
6. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_finn_linux_arm64_kos_prerequisite`
7. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_finn_linux_arm64_manual`
8. `next/tests/release/test_original_member_bridge.py::Bridge::test_actual_pair_receipt_failure_cannot_be_bypassed_by_signature`
9. `next/tests/release/test_original_member_bridge.py::Bridge::test_custody_replacement_after_pair_audit_is_detected_at_EOF`
10. `next/tests/release/test_original_member_bridge.py::Bridge::test_default_backend_and_actual_port_gap_before_any_candidate`
11. `next/tests/release/test_original_member_bridge.py::Bridge::test_delivery_reobserves_physical_originals_before_snapshot`
12. `next/tests/release/test_original_member_bridge.py::Bridge::test_delivery_root_rotation_cannot_relabel_existing_metadata`
13. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_aggregate_equality_object`
14. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_aggregate_same_subclass`
15. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_aggregate_wrong_subclass`
16. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_member_equality_object`
17. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_member_same_subclass`
18. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_member_wrong_subclass`
19. `next/tests/release/test_original_member_bridge.py::Bridge::test_exact_original_lock_through_actual_port_and_verifier`
20. `next/tests/release/test_original_member_bridge.py::Bridge::test_exception_text_is_closed_and_all_readers_close`
21. `next/tests/release/test_original_member_bridge.py::Bridge::test_finn_planner_real_bridge_rejects_signature_before_config`
22. `next/tests/release/test_original_member_bridge.py::Bridge::test_fixed_private_port_composition_reuses_backend`
23. `next/tests/release/test_original_member_bridge.py::Bridge::test_image_cannot_use_small_snapshot_port`
24. `next/tests/release/test_original_member_bridge.py::Bridge::test_manifest_raw_bound_precedes_hash_and_crypto`
25. `next/tests/release/test_original_member_bridge.py::Bridge::test_missing_current_context_before_transport`
26. `next/tests/release/test_original_member_bridge.py::Bridge::test_missing_independent_pair_policy_cannot_be_replaced_by_signature`
27. `next/tests/release/test_original_member_bridge.py::Bridge::test_noncanonical_aggregate_id_refuses_before_current_and_transport`
28. `next/tests/release/test_original_member_bridge.py::Bridge::test_noncanonical_member_refuses_before_current_and_transport`
29. `next/tests/release/test_original_member_bridge.py::Bridge::test_original_digest_mismatch_before_R01_parser`
30. `next/tests/release/test_original_member_bridge.py::Bridge::test_original_transport_reader_requires_actual_held_reader`
31. `next/tests/release/test_original_member_bridge.py::Bridge::test_owner_expected_inventory_must_match_original_signed_rows`
32. `next/tests/release/test_original_member_bridge.py::Bridge::test_read_limit_exact_type_and_measured_bound`
33. `next/tests/release/test_original_member_bridge.py::Bridge::test_requested_raw_identity_before_crypto_or_physical_open`
34. `next/tests/release/test_original_member_bridge.py::Bridge::test_requested_snapshot_measured_limit_cannot_be_enlarged`
35. `next/tests/release/test_original_member_bridge.py::Bridge::test_runtime_admit_config_consumes_actual_original_without_effects`
36. `next/tests/release/test_original_member_bridge.py::Bridge::test_sealed_snapshot_does_not_become_live_authority`
37. `next/tests/release/test_original_member_bridge.py::Bridge::test_small_record_original_is_supported_without_widening_port`
38. `next/tests/release/test_original_member_bridge.py::Bridge::test_sole_existing_port_delegates_to_fixed_R01_backend`
39. `next/tests/release/test_original_member_bridge.py::Bridge::test_unknown_member_returns_no_delivery`
40. `next/tests/release/test_original_member_bridge.py::Bridge::test_unrelated_original_corruption_cannot_be_bypassed_by_lock_request`
41. `next/tests/release/test_original_member_bridge.py::Bridge::test_unsigned_refuses_before_member_open`

## Evidence required before the EB1/F1 source exit

Retain exact tested commit/tree/test and fixture SHA256 values, every selected ID and setup/call/teardown outcome, both Python versions, deciding source-body/code fingerprints, raw sanitized logs and restoration receipts. Setup/import/discovery errors never earn BODY RED credit. New methods append to existing CI-listed files; the bridge's original 41 IDs and four partitions remain intact.

Run named one-locus omissions in the actual loaded owner release-audit call, R01 comment-signature check and planner evidence publication; require the frozen deciding source assertions to fail, restore the exact original function, and rerun the same nodes. Never persist an omission to protected source files. Final complete U3/bridge/owner source cohorts must pass after restoration. The handoff return binds these receipts to its final head; this document does not invent a hosted CI run or independent review verdict.

## Still held after EB1/F1

- EB2 waits for Finn's accepted HOLD-1 lifecycle/publication interface, Cox's producer/classifier, and the named composed source head. Missing/unlinked/ambiguous truth stays UNKNOWN/HOLD; no resource-absence inference or blind retry is introduced here.
- F2 waits for EB2 and one exact current-main/HOLD-1/E-B composition, then complete restored source suites, causal receipts and independent Hale/Vera review. Finn is final composition custodian; Cox signs off his SQL bytes.
- Real Cortex root/purpose/bootstrap/signing, actual protected owner/host/UID/import/root/peer/clock providers, verified-image authority and both-host physical B-N1…9/Q3 proof remain separate explicit gates. A first hand install grants no waiver.
- Every eventual product effect still requires independently admitted current finite consent, original context/fences/deadline, single consumption and durable outcome through the existing owner history. None is issued by these source tests or read-only receipts.
