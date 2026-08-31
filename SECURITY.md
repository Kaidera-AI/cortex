# Security Policy

## Reporting

Report vulnerabilities privately to **security@kaidera.ai**. Do not open a public issue for
an unpatched vulnerability. You will get an acknowledgement within 72 hours.

## Posture

- API access is token-authenticated; tokens are stored **hashed**, never in plaintext.
- TLS for any non-loopback transport.
- No secrets in code, tests, fixtures or git history — enforced at review.
- The runtime is designed to need **no privilege**: no root, no OS-global state in the data
  path, everything repairable as the owning user.

## Supported versions

Pre-1.0: only the latest 0.x release receives fixes.
