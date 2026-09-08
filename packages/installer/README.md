# Cortex installer

Canonical source for `packages/installer` in the public Cortex projection. This
extends the dependency-free `0.0.1` public launcher with the standalone lifecycle.
Node >= 18 and Bun use the same implementation. Linux and macOS use Podman >= 5.0;
Linux requires rootless systemd cgroups and linger, macOS a running rootless
Podman machine. `podman-compose` and Python >= 3.10 are runtime prerequisites.

The default release payload remains unset. `cortex install` without an explicit
qualification manifest refuses with exit 2 until publication supplies a verified
release. Merely changing that default does not publish a release. This W1 source
checkpoint has no qualified published images or authentication/platform release
proof. `PAYLOAD` stays `null`; offline fixture passes are not installation approval.

```sh
cortex preflight --json
cortex install --payload /absolute/path/release.json --skip-openkai \
  --state-dir /absolute/private/cortex-state --project cortex-test --api-port 8602
cortex install --payload /absolute/path/new-release.json --skip-openkai \
  --state-dir /absolute/private/cortex-state --project cortex-test
cortex install --rollback --state-dir /absolute/private/cortex-state --project cortex-test
cortex backup --backup-file /absolute/path/backup.tar.gz \
  --state-dir /absolute/private/cortex-state --project cortex-test
cortex restore --backup-file /absolute/path/backup.tar.gz --confirm-restore \
  --state-dir /absolute/private/cortex-state --project cortex-test
```

`--payload` is an explicit local manifest trust decision. It supports HTTPS
archives and local `file:` archives for qualification. Remote downloads follow
at most five HTTPS redirects, including GitHub release asset redirects, and
require a final HTTP 200 within one 30-second deadline. Every redirect is
revalidated; credentials, fragments, control characters, insecure schemes and
remote `file:` hosts are refused. No authorization headers are forwarded, and
the manifest's archive SHA-256 remains authoritative. Exit 0 means
the runtime returned the required effect receipt; 1 means preflight failure;
2 means refusal or a failed lifecycle operation.

## Release contract

```json
{
  "schema": "cortex.release.v1",
  "version": "0.1.003",
  "source_revision": "<40 lowercase hex source commit>",
  "schema_revision": "<64 lowercase hex migration inventory identity>",
  "compatible_schema_revisions": ["<own identity>", "<other supported identities>"],
  "delivery_kind": "prebuilt",
  "platform": "linux/arm64",
  "installation_sha256": "<64 lowercase hex install.json digest>",
  "url": "<absolute HTTPS release asset URL or explicit local file URL>",
  "sha256": "<64 lowercase hex installation archive digest>"
}
```

The gzip archive preserves the validated source projection rooted at `packages/`, including executable
`packages/deploy/cortex-runtime` and `packages/deploy/release.json`. The embedded
JSON uses `schema: cortex.payload.v1` and must match the external manifest's
`version`, `source_revision`, and `schema_revision` exactly. Separate derived files
`packages/deploy/install.json`, `install-compose.json`, and `image-lock.json` bind
installation identity, image-only Compose, and platform-manifest locks. Every
original and derived file is checksum-bound; the pristine projection's original
bytes and manifest remain unchanged. Use the maintainer packager below, not an
ad-hoc tar command. PAX/GNU extensions,
symlinks, hardlinks, devices, FIFOs, duplicate paths, traversal, file/directory
collisions, invalid checksums and setuid/setgid/sticky members are rejected.
Compressed and expanded source archives are each limited to 256 MiB and 20,000
members; OCI images are not embedded in the installation archive. Legacy source-only
archives, including cached install/upgrade/rollback targets, cannot execute.

Maintainers first use the canonical projector from a clean committed source.
`package-release.py --projection ABS --output-dir ABS` still produces a distinct
source archive for maintainers; the installer refuses it. To derive installation
files, also supply `--images ABS --platform linux/arm64` (or `linux/amd64`). This
renderer requires the existing maintainer PyYAML 6.0.3; runtime requires only the
Python standard library. The empty private output directory must be outside the
projection. No build, pull or publication is performed by packaging.

The image JSON has schema `cortex.images.v1`, the exact `version` and
`source_revision`, and `platforms` mapping supported Linux platforms to all seven
roles: `api`, `db`, `tls`, `provider`, `embed-worker`, `graph-worker`, `pdf-worker`.
Each role supplies `repository` (fully qualified registry/repository), `tag`, and
`manifest_digest` (`sha256:` plus 64 lowercase hex characters). Migrator reuses
`api`; backup and PKI restore reuse `db`. Unknown or missing roles fail closed.
Each digest must identify the selected **platform manifest**, not a config/image
ID or multi-platform index. Maintainers resolve indexes before writing locks;
the runtime checks the resulting manifest, repository, platform and source labels.
The Podman Linux backend determines platform: Apple Silicon uses `linux/arm64`,
not `darwin/arm64`. Supplied locks are integrity inputs, not qualification receipts.

All archive members are validated before extraction. Runtime code is invoked
only after the downloaded archive digest, embedded identity and extracted file
bytes have been verified. Cached releases are checked again before every use.

Runtime protocol:

```text
cortex-runtime prepare-images|up|check|schema-status|backup|restore
  --project NAME --state-dir ABS --payload-dir ABS --api-port PORT
  [--schema-revision INVENTORY_SHA256] [--rollback]
  [--backup-file ABS] [--confirm-restore]
```

`prepare-images` is a private launcher protocol action: it may acquire missing
digest-pinned images, verifies all seven roles and returns `images_prepared: true`,
`platform`, and `source_revision`. It changes no database, writer or provider state.
It runs before a pending journal is created; acquisition failure is retryable.
`up` rechecks the same identities before stopping writers. Generated Compose has
no build entries and uses `pull_policy: never`; no source-build fallback exists.
Recovery/maintenance helpers require verified cached images without implicit pulls.

Diagnostics go to stderr. `check` returns one JSON object on stdout with
`healthy: true`, `schema_revision` and `discovery_url`. Before an upgrade, retry
or rollback, `schema-status` returns the actual database `schema_revision`
without requiring a healthy API or worker. It is read-only and does not run
migrations. After deployment, full `check` still requires health and the exact
expected identity. Other lifecycle operations
return their JSON receipt. The runtime owns database/container operations.

## State, upgrades and recovery

Default state is `~/.local/share/cortex`; select a dedicated private directory
with `--state-dir`. Project and API port are persisted. Directory mode is 0700,
state/archive files 0600. An exclusive lifecycle lock prevents concurrent
installation and backup/restore. State JSON is written using fsync plus atomic
rename. `current` and `previous` point to verified archive identities;
`pending` is written after image acquisition and before database/writer mutation,
and retained on failure.

Upgrade requires the target's declared compatibility list to include the actual
current schema. Rollback selects the previous verified payload, checks that it
supports the retained database inventory, and invokes `up --rollback` without
migrations. Reinstalling rolled-back code preserves that newer inventory. After
a failed upgrade, rollback selects the last successful `current` payload. Schema
discovery prefers that verified current helper, which reads the complete actual
ledger (including newer IDs) even if pending image acquisition was interrupted.
No code operation reverses schema. Restore is a separate explicit command.

A failed upgrade can be retried with the same manifest. If a process was killed,
inspect `install.lock`, its recorded PID and the pending state before removing
only that stale lock. A database whose schema cannot be observed blocks upgrade,
retry and rollback until schema discovery is restored; unhealthy API or worker
processes alone do not block a schema-compatible rollback.
Archives, previous releases and failed extraction stages are retained; there is
no automatic destructive cleanup of user state.

## Shared provider settings

OpenKai's committed contract is `$OPENKAI_HOME/.env`, default `~/.openkai/.env`.
Existing owner-private files are adopted byte-for-byte with no chmod or rewrite.
Missing settings are created exclusively from the exact `.env.example` at
OpenKai commit `f3660f3c19939d2a6ff3b95be9aab3f85fb8312a`, SHA-256
`fd25fc78a15f85a47fa8bae91179dcea3892cc37f81cd42186face03e962fc64`.
All provider/model examples remain comments: no provider choice or credential is
invented. A separate `cortex-provider-bootstrap.json` receipts the first author.
The installer state records only the path in `provider-settings.json` for backup.

First use offers a skippable recommendation to install OpenKai. Unattended first
use passes `--skip-openkai` explicitly. An existing settings file needs no prompt.
This materialiser does not implement provider resolution or edit credential keys.

## Verification

```sh
node --test tests/installer.test.js
bun test tests/installer.test.js
```

Tests exercise the full launcher process against a synthetic runtime: install,
forward upgrade, schema-preserving rollback, failed-upgrade retry/rollback,
backup/restore forwarding, cache tampering, archive attacks, exclusive state
locking, first-author/provider adoption, bounded HTTPS redirects, transport
cleanup and refusal of unreadable database inventories. Rootless Podman integration and
fresh/seeded database qualification are separate required release evidence.
