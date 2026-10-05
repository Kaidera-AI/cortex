# Cortex member reader v1: macOS and Linux

Public API: `cortex_v2.clients.member_reader.MemberKeyReader`. Standard library
only, Python 3.12. Supply every identity explicitly:

```python
reader = MemberKeyReader(
    installation=installation_id, project=project_key, name=console_member_name,
    project_root=physical_project_root,  # pathlib.Path, absolute, unlinked
)
headers = reader.headers()  # immediately before each logical HTTP request
```

KOS selects the installation and physical project root from its verified
connection/project descriptor, and supplies the enrolled **Console member**
identity. It never selects an owner/lead/recovery default, request-supplied
identity, environment bearer, other project's key or a scanned directory.
Identifiers are bounded ASCII slugs; reserved privileged names are refused.
An opaque token cannot prove its grant role locally: verify the selected
identity's member role using the server's native principal operation before
admitting the connection. Owner/admin operations remain the separate helper.

## Custody and platform

The store root is `<project>/.kaidera/secrets/`, owner-only 0700. Linux stores
0600 JSON key records beneath `<installation>/<project>/<name>.key`. Darwin
uses only `<installation>/cortex.keychain-db`, owned 0600, on an internal
ownership-enabled volume. Its item service is `Cortex <installation>` and
account is `<project>/<name>`; the encrypted item contains the token, manager
and API-issued expiry. It is a dedicated local file keychain, never the login
keychain, ambient search list, iCloud keychain or Mac file-token fallback.

Provisioning remains the credential manager's task. It creates/unlocks the
dedicated keychain explicitly via `KeyStore.initialize_keychain(password)` /
`unlock_keychain(password)`; these accept bytes in process memory, never argv
or environment. Creation refuses an existing store and excludes its root from
Time Machine. Enrollment must admit the exact released consumer to the item's
Keychain ACL; a different/untrusted executable refuses headlessly. The reader
never changes ACLs or unlocks/prompts automatically. This module does not
implement the privileged owner helper or grant the Console its authority.

Credentials are excluded from local backup and reissued after restore. KOS's
FileVault/export/backup layer must exclude the secrets folder on Linux as well
as Mac. Missing secrets are an enrollment condition, not grounds to restore or
copy another identity's keys.

## Read, rotation and failure boundary

`headers()` atomically reads one token/metadata record **on each outgoing
logical request**, without a bearer cache. It returns only `Authorization:
Bearer <key>` and `X-Cortex-Scope: <project>`. The caller must never log/repr,
serialize into UI/status or persist this dictionary. Keys are the API's
43-character base64url bearer format; legacy ctx1 values are refused.

Missing, unsafe, locked, access-denied and expired records raise
`KeyStoreError` before transport. No keychain, directory or credential is
created by a missing-key read. No unlock, renewal, scheduled rotation or
issuance is implied. A manager's atomic replacement is observed by the next
request. Remote revoked/expired/403 responses retain explicit v2 selection;
they are returned as failures, with no automatic retry, owner/lead lookup,
v1/admin fallback or scope widening. Preserve `Cortex-Key-Expires` when present.

KOS owns origin/transport validation: use the verified configured Cortex
origin; refuse redirects to another origin and do not send credentials to a
proxy or request-supplied URL. Writes preserve the native/facade operation's
required `Idempotency-Key`, payload, claim fence and error/outcome semantics.
The reader does not fetch routes or authorize transport replay.

## Distribution from frozen bytes

`scripts/release/build-key-reader.py` produces a deterministic
`cortex-key-reader-<source-sha>.zip` and `reader.json` containing its SHA256,
module/API and every file digest. It requires a clean frozen source tree.
KOS verifies and vendors that exact zip during its build, then imports it from
its packaged Python. No pip install, build, Git checkout, download or latest
lookup occurs on the installed target. The archive contains only the reader,
Mac/file stores, shared lifetime function and package markers; no server/DB
dependency. Version 1 is the module/API contract; exact source/archive digests
in the release receipt bind its implementation. Source acceptance and the
consumer's released integration remain separate gates.

## Native fixture proof

The narrow fixture at `tests/test_member_key_reader.py` creates its own
temporary project and synthetic credentials: one real dedicated Mac keychain
and one native Linux file store. It checks missing/no creation, reserved
identity refusal, exact member headers, next-request rotation, other-project
refusal, expiry and deletion. Mac also checks exclusion and locked/headless
refusal/explicit unlock, then deletes only that temporary keychain. It never
reads an installed/login keychain or real credentials. The fixture is executed
again importing the frozen zip, to prove the consumer receives those bytes.

This contract freezes the credential/header boundary. It does not claim the
remaining legacy facade routes are served; their caller rows require their
own package receipts before the live switch.
