# Cortex v2 candidate metadata draft — 21 October 2026

Status: **IDENTITIES RULED; AGGREGATE INCOMPLETE**, 16 October 2026.
Kai accepted these four NEW identities in the 05:00 ruling and Cox C1 handoff
`ac100097-846a-4421-b6e9-ef512e5ec27b`, binding the original identity record
`192c9cba7601e7f50c2557dda511f7e33b3b2dcf`. This child, under cut-plan handoff
`ccbca3df-47d2-4816-994c-5a40210324f2`, adds only the existing R01 constants
`schema=cortex.release-aggregate.v1` and `format_version=1`. It grants no
signing, original-material, installed or publication acceptance.

| R01 JSON pointer | Ruled literal | Convention and choice |
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

This file is a canonical, incomplete R01 **metadata fragment**. Its release object
uses the existing closed release grammar. The schema and format constants are
now present. A complete aggregate
still requires `purpose`, `sequence`, `not_before`, `expires_at`, `compatibility`
and measured `members`. No sequence, timestamp, signature, material digest,
owner policy
or historical OLD tuple is synthesized here. The full aggregate validator must
continue to reject this incomplete planning fragment.

Cox C1 resumes against the ruled original identity record. The eventual cut
must preserve the accepted `/release` values consistently in its independent
policy, runtime lock, compatibility original and Core/API build/descriptor
originals. It retains the existing R01 admission and signing gates.

## Remaining owner deliveries

| Absent top-level field | Delivery required before assembly/signing |
| --- | --- |
| `purpose` | Cox/Kai release policy selects the existing TEST or production scope; CTO grants the exact signing purpose. Pre-release status alone does not select one. |
| `sequence` | Release owner records the current floor and next non-replayed sequence. |
| `not_before`, `expires_at` | Release owner supplies the actual cut validity window, checked against independent policy/current time; existing seven-day maximum and tighter caps remain. |
| `compatibility` | Cox supplies accepted descriptor/schema/event/migration/operations originals and their exact member references; R01 and PG-C epoch domains stay separate. |
| `members` | Ren-KOS supplies the four actual Core/API ×linux/amd64+linux/arm64 archives, external runtime lock and required record closures; R01 measures original paths/lengths/digests and per-image receipt joins. |

The final source commit/tree is rebound only after source composition and review.
No placeholder, empty list, fixture hash or guessed owner context fills these gaps.
Public `assemble_aggregate` remains UNBOUND at the inspected main baseline; its
independent producer/owner composition must be delivered through the existing R01
path before a real signed aggregate can be claimed. This draft adds no binder.

H-D569 places AG native helpers, PG-F, physical B-N1..9 and installed F01 proof
first in the next release, openly declared and with zero PASS credit. It does not
make absent archive/manifest bytes exist. H-D428 still requires paired distribution
from one source/version/manifest; H-D464 requires the release-bound guides. No
native installation or upgrade support is asserted by this metadata fragment.

LEAN: reused R01 fields, version style, canonical codec and validators; did NOT
add a parser, schema, runtime configuration or metadata consumer; could delete or
merge none.
