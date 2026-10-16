# Cortex v2 candidate identity proposal — 21 October 2026

Status: **DRAFT FOR KAI RULING**, under handoff
`112e51f7-3dcd-4267-a2c5-753747d6baee`, 16 October 2026.
Finn proposes the following literal NEW identities for the Wednesday 21 October
pre-release candidate. Kai owns acceptance and the subsequent Cox C1 direction.

| R01 JSON pointer | Proposed literal | Convention and choice |
| --- | --- | --- |
| `/release/release_id` | `v0.2.001` | Cortex's published v2 TEST tags use the numeric stem `v0.2.001` with `-test.<date>.<run>` suffixes. The earlier `v0.1.001` and `v0.1.002` tags use the same three-digit patch style. Select the existing v2 stem as this candidate's NEW identity. The existing U13 `_version` accepts numeric `vX.Y.Z` and rejects suffixed labels, so candidate date/channel stay in this proposal rather than a release-ID suffix. |
| `/release/api_contract` | `cortex.core-api.v1` | **New default; no adopted R01 release API-contract literal was delivered.** Reuse the `cortex` prefix and `.v1` namespace style of `cortex.release-aggregate.v1`, plus R01's existing `cortex/core-api/production` scope. This names the first proposed Core/API release contract. It does not adopt the document-only OpenAPI draft version or establish compatibility acceptance. |
| `/release/core_module_version` | `0.2.001` | **New, independently selected Core default.** Core's recipe source envelope leaves `module_version` UNBOUND on both targets. Use Cortex's existing numeric version style for the initial v2 Core module; the R01 build target and actual Core descriptor/API descriptor must later carry this exact value. |
| `/release/api_module_version` | `0.2.001` | **New, independently selected API default.** API's recipe source envelope likewise leaves `module_version` UNBOUND. Select the same initial numeric value for API explicitly; no rule derives it from Core or from `release_id`. Later API build/descriptor originals must agree exactly. |

The selected strings occur in [release.json](release.json) under the existing R01
`/release` pointers. Its `schema_version=1` and `event_version=1` retain the R01
metadata domains enforced by `release/metadata.py`; PG-C's separately ratified
U13 deployment epochs 1/2 do not change these domains.

## Measured convention sources

- Published Cortex records, inspected 16 October 2026:
  [v0.1.001](https://github.com/Kaidera-AI/cortex/releases/tag/v0.1.001),
  [v0.1.002](https://github.com/Kaidera-AI/cortex/releases/tag/v0.1.002), and the v2
  [4 October](https://github.com/Kaidera-AI/cortex/releases/tag/v0.2.001-test.20261004.37205559175),
  [5 October](https://github.com/Kaidera-AI/cortex/releases/tag/v0.2.001-test.20261005.37249460638),
  [6 October](https://github.com/Kaidera-AI/cortex/releases/tag/v0.2.001-test.20261006.37391824107)
  TEST releases. None of these five published records is the proposed unsuffixed
  `v0.2.001` Core/API aggregate.
- [R01 aggregate contract](../../../docs/release/R01-AGGREGATE-CONTRACT.md),
  [aggregate schema](../../../contracts/release/aggregate.schema.json) and
  [policy schema](../../../contracts/release/aggregate-policy.schema.json):
  existing release fields, namespace and Core/API purpose, with independent
  policy/lock/member joins.
- [Core source envelope](../../recipes/core/source-envelope.json) and
  [API source envelope](../../recipes/api/source-envelope.json): both targets'
  module-version facts are explicitly UNBOUND at the inspected baseline.
- [Metadata admission](../../../src/cortex_core/release/metadata.py),
  [aggregate pairing](../../../src/cortex_core/release/aggregate.py) and
  [U13 release parser](../../../src/cortex_core/upgrade.py): exact module joins,
  metadata version 1 and numeric release ordering.
- [OpenAPI info](../../../contracts/openapi.json) declares
  `0.1.020-c01-draft.1` and document-only review status. It supplies no adopted
  R01 API-contract literal. The launcher
  [payload record](../../../../packages/deploy/release.json) is
  `cortex.payload.v1` / `0.1.002`; the npm launcher is `0.1.2`. Neither supplies
  the four NEW identities. KOS's separate `0.2.010` version is excluded.

## Source and later cut

The draft records the measured source baseline, Cortex main
`1d1806e9a421d92238f8dcab0b87a442d4c8bf25`, tree
`7aa6c2365150c0bb1b40649c1a3101c5d5979ce2`. That is the source inspected for
this proposal. The later release cut must bind its actual final F commit/tree
after the remaining source composition and verification.

This file is a canonical R01 **release/source fragment**. Its release object uses
the existing closed release grammar. A complete aggregate still requires the
existing schema, format, purpose, sequence, validity, compatibility and measured
member fields. No sequence, timestamp, signature, material digest, owner policy
or historical OLD tuple is synthesized here. The full aggregate validator must
continue to reject this incomplete planning fragment.

Kai's ruling on these literal values precedes Cox's resumption. The eventual cut
must preserve the accepted `/release` values consistently in its independent
policy, runtime lock, compatibility original and Core/API build/descriptor
originals. It retains the existing R01 admission and signing gates.

LEAN: reused R01 fields, version style, canonical codec and validators; did NOT
add a parser, schema, runtime configuration or metadata consumer; could delete or
merge none.
