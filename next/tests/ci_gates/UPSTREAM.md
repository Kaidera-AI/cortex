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

Day-2 join reuses existing `.github/scripts/pytest_next_worker.py` phase emission,
`pytest_next_offline.py::receipt_valid` and the existing hosted `pytest-pg` job.
Nix native fixture dependency: 0cf8e85e846de7a921c5d9337dbc83f356b82615;
fixture bytes are preserved, not vendored or modified by Mike.

Signing admission reuses Nix's unchanged 22-method source suite at
65fbe921e38684df8cd96f10afb07ba054b846aa and stock minisign 0.12.
Upstream: https://github.com/jedisct1/minisign/releases/tag/0.12 ; ISC licence.
The upstream Linux archive SHA256 is
9a599b48ba6eb7b1e80f12f36b94ceca7c00b7a5173c95c3efc88d9822957e73.
Its detached signature was verified before pinning with the public key in the
upstream README at b85e15d (2026-10-17); raw archive/signature/verification are
retained in the sign-admission evidence. CI checks the archive before extracting
only its x86_64 binary under runner.temp and requires `minisign 0.12`.
No vendored binary, system package or release-signing key is added. This tool
signs only disposable source-test fixtures; no installed-candidate credit.
