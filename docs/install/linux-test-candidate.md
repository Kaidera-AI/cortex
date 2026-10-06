# Cortex v2 Linux x86_64 candidate

## Status and admission

This source checkpoint builds Linux host executables and five AMD64 OCI role
archives. It is an unsigned internal TEST candidate. The Linux provisioning,
CM-2 descriptor and external signed release manifest await Kai's consumer
contract ruling. **There is no admitted stranger-install command at this
checkpoint.** The bundled lifecycle installer still refuses Linux installation;
its Linux `--help` is checked as an executable entry point.

The intended stranger host is Rocky 10, x86_64, with SELinux Enforcing and
rootless Podman. Admit a guide install only after all of these receipts exist:

1. Vera accepts the exact source and dispatch route.
2. Kai admits one first-attempt native Linux CI run through `dispatch-once.py`.
3. CI qualifies the actual archive bytes, all embedded ELF libraries, both help
   entry points, every OCI role, migrations and restart smoke.
4. The CTO signs the exact build using the release key shared with KOS. Verify
   the external manifest and archive against that public key before extraction.
5. The accepted CM-2 owner provisioning and native member readiness commands
   bind the exact local Cortex installation and Console member to KOS.
6. Perform the signed-byte guide install and restart smoke on the clean kos-test
   VM. If it fails, fix, rebuild and re-sign before another rehearsal.

No VM, shared engine, live ledger, credential or unsigned package installation
is authorized by this source checkpoint. The Mac Keychain ACL admission gate
remains a separate Mac obligation.

## Podman prerequisite

Use Linuxbrew's **current** Podman formula, with Homebrew's bottle checksum
verification. Once Linuxbrew is installed for the operator, run:

```sh
brew update
brew install podman
if test -n "$(brew outdated podman)"; then brew upgrade podman; fi
podman version
brew info --json=v2 podman
```

Record the provider receipt, actual client and server versions and native
architecture. Both stable versions must be **6.0.2 or newer**. There is no
upper version ceiling. The dated denylist in `scripts/release/podman_policy.py`
currently has no entries; an explicit denied version is refused with its date
and reason. Future signed CM-2 manifests bind this policy on both consumers.

Do not use the old September Docker/npm setup. If the qualified Linuxbrew
engine encounters SELinux or rootless trouble on kos-test, return a consult
with the Rocky distro alternative. Keep SELinux Enforcing; do not change file
labels, disable policy or substitute a privileged engine as a workaround.

## Native build evidence

All Linux workflow jobs use Ubuntu 22.04 x86_64. The host job compiles the
accepted CPython 3.12.14 and OpenSSL 3.5.8 sources in a fresh prefix, with system
GCC and a controlled library environment. It installs only hash-locked builder
wheels, never target dependencies.

`linux_binaries.py` inspects the literal outer ELF and every ELF in the
PyInstaller archive, including recursively embedded ZIP members. It requires
little-endian ELF64 x86_64 and a maximum **required** GLIBC version of 2.35;
definitions do not establish requirements. Unknown reports, malformed or
unsafe archives, private GLIBC requirements and unsupported architectures
refuse before executing help. Both `cortex-test --help` and
`cortex-agent --help` must pass before final host inventories are written.

The AMD64 base manifests are independently resolved for the same Python and
pgvector version tags as the Mac image recipes. Automatic recipe parity allows
only architecture digest substitutions. Assembly rehashes the downloaded OCI
manifests, configs, layers and host binaries; it does not rebuild them.

The Linux route is fixed to `cortex-linux-candidate.yml` and
`ren-cx/linux-package-build-admitted-<full SHA>`. The guard checks both workflow
files for overlapping push predicates and rechecks their exact bytes before
each provider mutation. A matching push creates its ref once and prohibits a
manual dispatch, including when no run is visible yet. A normal source-review
branch does not trigger the native build.
