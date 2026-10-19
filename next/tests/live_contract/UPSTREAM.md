# Upstream and reuse

Cortex source pin: 7a1078213ac2497d6219e7778c7866001f97f30b; MIT, copyright 2026 Kaidera contributors.
Repository root LICENSE retains the full notice and disclaimer.
Python standard library: PSF licence; existing Python 3.13.7 and 3.14.0,
no vendoring or new package installation.

Reuse: existing Cortex strict JSON/refusal helpers and C01 DRAFT as rejection
fixture; OpenAPI 3.1 metadata only. No schema library or HTTP implementation copied.
Schemathesis 48a5a9de810c45215fe75d3dfe3e3fdf4966a420 (MIT), with Hypothesis
(MPL-2.0), remains the accepted day-2 generation candidate. Neither is newly
installed or vendored here; transitive licence resolution remains that integration's
obligation. Existing JSON Schema validation is not reimplemented.
LEAN: reused one export and standard OpenAPI Links; no API service, parser/fuzzer
platform or acceptance mock server. Could fold metadata checks into the existing
schema-loader seam when day-2 request generation is integrated.
