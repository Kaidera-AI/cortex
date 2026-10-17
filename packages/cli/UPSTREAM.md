# HTTP helper provenance

- Donor: this repository's `packages/cli/_cortex_api.sh` at Cortex
  `7a1078213ac2497d6219e7778c7866001f97f30b`, retained Day-1 Finn branch
  `bc4514a66164c9a5cbee449d7ff5bd2dc839d74c`. Repository MIT license applies.
  Reused function names, shell URL encoding and stock curl invocation. A-source
  changes replace legacy discovery/admin fallback and result flattening.
- Curl is an external system executable, not vendored. Author source controls
  observed `/usr/bin/curl` 8.7.1 (x86_64-apple-darwin26.0, SecureTransport).
  Its curl license and exact both-target executable/package closure belong to
  Nix/Bob's packaging receipt; this host observation supplies no installed credit.
- Python uses only stdlib framing, process supervision and JSON validation;
  no Python networking library or additional package is introduced. Test parents
  are Python 3.13.7 and 3.14.0; the helper's existing `python3` command resolved
  to Python 3.14.0 during these author runs. Earlier parent-only repetitions did not prove two helper closures. The later
  BOUND_RUNTIME and PROCESS receipts prepend each interpreter directory to
  PATH and freshly record helper `python3 -V` as 3.13.7 or 3.14.0. These remain
  host source controls, not paired installed/native qualification.
- Framing/custody interface: Cox's accepted
  `review/cortex/design/J06-interface-v1.md`, SHA256
  `25f208b916fa71f7c42be52399c0a92b1be1b2241d412427ab395712893d6d49`.
  The proposed health/receipt wire is separately OPEN, not implied by that hash.
