# Native Mac host bootstrap for Cortex TEST CI

The macOS-14 arm64 job compiles CPython 3.12.14 and OpenSSL 3.5.8 from fixed
official HTTPS archives. The bootstrap verifies each source SHA256 before
extracting or executing it. It targets macOS 14.0 and builds with two make
jobs, standard OS tools and disabled Homebrew/pkg-config discovery. No global
runtime is installed and no host binary is copied from a developer Mac.

The fresh job-private runtime contains its own prefix and venv. The host
freeze uses that venv's exact interpreter for pip, PyInstaller and archive
inspection; it validates the bootstrap input/provenance binding and copies
the native runtime's upstream licence notices into the resulting host
inventory. The system public CA bundle is explicit for builder pip. The
existing six-wheel hash lock is unchanged. The frozen executable must launch
its help command on the native runner before upload.

The source-review branch does not start package builds. After Vera accepts
the exact source, the SHA-named admission branch starts one new first-attempt
workflow identity. The workflow still uses public standard runners, matching
source/version image rehearsal and host receipts, and assembly from those
exact uploaded bytes. A failed attempt is retained rather than rerun under
its old identity. Each newly assembled candidate requires its own literal
guide install and restart smoke before its authorized TEST pre-release.

The bootstrap's four focused contract checks prove fixed inputs/job wiring,
bad-download digest refusal and sanitized tool discovery. A local native
runtime build is distinct from a completed hosted CI workflow; neither is a
signed release, target install, Mac switch or consumer integration receipt.

Input sources are the fixed archives under
[CPython 3.12.14](https://www.python.org/ftp/python/3.12.14/) and
[OpenSSL 3.5.8](https://www.openssl.org/source/openssl-3.5.8.tar.gz).
The minimum OS is a compiler target; the signed release keeps its separate
closure, CI, package, qualification, signing and publication gates.
