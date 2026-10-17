# Option A config foundation

The public contract is [contract.md](contract.md). Run the foundation units with
`PYTHONPATH=next/src python3 -m unittest discover -s next/src/cortex_core/runtime/tests -v`.
Readiness units live in `next/src/cortex_core/schema/tests`.

One vault config is read once for an explicit purpose. The frozen result exposes
only its admitted DB role references, or the client reference in client mode.
The file resolver walks directory file descriptors without following links,
checks the opened regular inode's owner/mode/link count/size, reads with a byte
bound and checks that the file did not change while read. It never logs bytes.

The host decoder runs on stdlib-only Python (`-S` is exercised by the units).
Schema consumers still load the existing pinned jsonschema implementation.
No command dispatcher or listener is delivered by this Day-1 slice. No result
from these unit tests qualifies a real database, secret custody, install or
paired-platform release.
