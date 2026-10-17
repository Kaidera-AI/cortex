# Upstream and reuse

Cortex source pin: 7a1078213ac2497d6219e7778c7866001f97f30b; MIT, copyright 2026 Kaidera contributors.
Repository root LICENSE retains the full notice and disclaimer.
Python standard library: PSF licence; existing Python 3.13.7 and 3.14.0,
no vendoring or new package installation.

Reuse: .github/scripts/pytest_u13_receipt.py ReceiptRefusal/require/strict JSON
helpers and next/tests/test_receipts.py AssertionResult/report/classify at the pin.
Git and gh remain existing command-line dependencies. GitHub Actions metadata API:
https://docs.github.com/en/rest/actions/workflow-runs and
https://docs.github.com/en/rest/actions/workflow-jobs (read 2026-10-17).
No provider documentation/code copied. All run attempts use attempt-specific endpoints.

mutmut e687da39dccc3e94aa165f8f5037fe4b24a3a3be (BSD-3-Clause) and
pytest-reportlog a8ef021cf6250a0ed8c26bac2ac74b98474c2e8a (MIT) remain
reviewed candidates only. Neither is installed, vendored or used as the kill oracle.
LEAN: reused strict runner/classifier; no quality service, second outcome parser or
automatic green retry. Could consolidate phase-report emission into the original
runner after that owner's review; this dispatch does not edit it.
