# Cortex v2: install the macOS arm64 TEST package

This package is a side-by-side TEST instance with scratch data. It does not
migrate, switch, stop or update an existing Cortex or KOS installation.
The first archive is unsigned TEST material; it is not the signed customer release.

## Requirements

- macOS 14 or newer on Apple Silicon, a normal user account.
- Podman client **and machine/server 6.0.2 or newer**, with a running arm64 machine and
  an explicit rootless connection.
  The qualification baseline is 6.0.2. Use the official Podman installer;
  KOS does not install or replace this prerequisite.
- At least 2 GiB free in the destination and 2 GiB available in the machine;
  allow 2 GiB of additional machine memory for this TEST stack.
- The frozen SHA must have passed Vera's package-source review before the
  admitted build and this installation. Use only the resulting exact archive.

The native `cortex-test` executable contains its Python runtime. No Python,
pip, npm, checkout, local image build or Compose provider is needed here.
The package includes the OCI archives and immutable image inventory.

## 1. Verify and unpack

Download the exact versioned archive and its external `SHA256SUMS` from the
admitted CI candidate, or the GitHub pre-release after its smoke gate passes.
Work in an otherwise empty download folder. The release page supplies the
literal versioned filename; do not use a `latest` URL.

```sh
shasum -a 256 -c SHA256SUMS
tar -xzf cortex-<version>-macos-arm64.tar.gz
```

Checksums establish byte agreement with the reviewed TEST receipt; this
unsigned candidate does not claim independent signature authentication.
The extracted folder contains `release.json`, an internal checksum inventory,
`bin/cortex-test`, OCI archives, SBOM/package inventory and this guide.

For this unsigned TEST archive, macOS may ask you to allow the executable in
System Settings → Privacy & Security after its first launch. Confirm the exact
download/checksum receipt first, allow only this candidate, then repeat the
command. A notarized customer installer is a later release gate.

Set two **absolute paths**, using the actual extracted version and user name:

```sh
package=/Users/<your-user>/Downloads/cortex-<version>-macos-arm64
test_root=/Users/<your-user>/.cortex/test/cortex-v2-package
```

The installer refuses linked paths, existing TEST roots, an occupied TEST
namespace, protected ports, wrong platforms, image/file mismatches and an
ownership-disabled volume. The runtime root must be a candidate subfolder
under `/Users/<your-user>/.cortex/test/` on an internal volume, excluded from
Time Machine; never use a synced folder or the shared SSD
for this runtime root. Public downloaded artifacts may be staged on the SSD.

## 2. Select the running Podman connection

```sh
podman system connection list
```

Choose the existing running arm64 Cortex-owned machine's connection. Do not
restart or resize a shared machine. If it is stopped, its owner starts it and
verifies usable `podman info` before installation. Until the release login
starter is qualified, the existing machine starter/operator remains in use.

## 3. Verify the package, then install

Substitute the exact connection name from the listing:

```sh
"$package/bin/cortex-test" verify --package "$package" --root "$test_root"
"$package/bin/cortex-test" install --package "$package" --root "$test_root" \
  --connection <connection-name> --port 18601
```

Port 18601 must be free. Ports 8501, 5499 and 5500 are always refused.
The database has no published host port. The TEST service label is
`ai.kaidera.cortex.TEST-v2.<installation-id-without-hyphens>`. Each installation
gets its own `cortex_v2_package_test_<installation-id-without-hyphens>` resource
namespace and ownership label; the fixed TEST application profile is separate
from resource names.

The installer loads verified image bytes without pulling/building, creates
private synthetic credentials, starts the new database, waits for it, completes
the finite migration, then starts API/workers. Its only supported resources
are its own labelled TEST objects. It prints release/source/path/status,
never credentials. It does not change the default Cortex endpoint or KOS
prerequisite descriptor.

Success is a `TEST ready` JSON line with the exact version, source and URL.
A refusal exits 2 and preserves the partial candidate. Its private install
record is written before package staging, and `status` works even if the copy
failed. Clear it only with the explicit confirmed `erase` command below. Do not read container Env, full
Config, logs or credential files to troubleshoot. Give the safe refusal and
the stage/status receipt to the Cortex owner.

## 4. Run the packaged smoke and lifecycle commands

```sh
installed="$test_root/package/bin/cortex-test"
"$installed" status --root "$test_root"
"$installed" smoke --root "$test_root"
"$installed" stop --root "$test_root"
"$installed" start --root "$test_root"
"$installed" smoke --root "$test_root"
```

The smoke requires public readiness, an unauthenticated read refusal and an
authenticated synthetic write/read round trip. It writes only scratch data.
`migration-receipt.json` and `smoke-receipt.json` are public, token-free
receipts inside the TEST root. Stop preserves data/credentials/images; start
uses only installed bytes and re-verifies migration checksums before workers.
The second smoke after restart is required before this local TEST is admitted.
These receipts do not establish full R01–R28, migration, live switch or Linux
qualification.

## Retirement

Stop the TEST stack first. Its data and credentials remain by default.
When the Cortex owner authorizes retirement, the packaged `uninstall --root`
command removes only stopped containers and their network with this install's
ownership label. It retains pgdata, credentials, images and the TEST root.
Start cannot repair a retired instance. Never run Podman prune, a prefix
deletion or a live-stack command. Full erasure is explicit and permanent. Use the original extracted executable
if staging failed, and obtain the installation ID from its public status:

```sh
"$package/bin/cortex-test" status --root "$test_root"
"$package/bin/cortex-test" erase --root "$test_root" --confirm <exact-installation-id>
```

Erase validates the complete existing object's labels before changing anything,
stops/removes only that installation's containers, and removes its network,
pgdata, all eleven secrets and private TEST root. Any foreign label refuses.
Only images newly loaded by this installation are eligible for removal; an
image still used by another running or stopped container is retained. There
is no force or prune. It prints a public erasure receipt. This is also the
recovery route for a partial TEST installation, so a later candidate needs
no hand-written Podman commands. Keep the extracted package until cleanup ends.

## Builder/reviewer sources

The native Linux arm64 and macOS arm64 jobs use GitHub's
[standard public runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The host build uses the hash-locked PyInstaller toolchain and its
[native platform requirements](https://pyinstaller.org/en/stable/requirements.html).
Build tools run only on the builder, not on the target Mac.
