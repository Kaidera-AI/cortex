# H-D449 producer epoch delta

Accepted scope: Kai's r42609:04 ruling of2026-10-10, after Ren-KOS's complete
attempt5 layer classification. The accepted six-file350a producer is the pinned
tooling baseline; the graph product source is separate and remains under Cox.

1. Freeze a controller test before the fix. The committed original350a producer
   must fail for absent build clock and unprepared checkout mtimes.
2. Set the release epoch to1791586380, the verified committer time of e9a19f14.
   Prepare only tracked files, symlinks themselves and their parent directories
   in the separate clean source checkout before any build. Leave Git metadata,
   ignored files, bytes, modes, ownership and symlink targets untouched.
3. Pass the fixed epoch through all seven planned build arguments and the
   requested `--timestamp` option. Preserve role order, contexts, identity,
   stores, arch, archive verifier, output format and every admission gate.
4. Prove the graph-controller call site prepares inputs before its first build.
   Actual native startup and fourteen-control admission are excluded from this
   local intercepted test; the real attempt6 must prove them independently.
5. Return the tooling delta and the candidate graph-controller diff for Vera.
   No attempt6 before both Cox's product delta and this tooling are accepted.
   Two independent full-byte-equal native exports and all downstream gates remain.

## Necessary flag correction for Kai/Vera

The [versioned Podman5.8.2 manual](https://docs.podman.io/en/v5.8.2/markdown/podman-build.1.html)
describes mutually exclusive clock options: `--timestamp` sets image metadata
and newly committed file timestamps; `--source-date-epoch` selects a different
clock mode, with optional clamping through `--rewrite-timestamp`. Combining both
clock flags is invalid, even if their numeric values agree.

This proposal retains the explicitly requested `--timestamp 1791586380` and
supplies `SOURCE_DATE_EPOCH=1791586380` as an explicit build argument. The Podman
process receives the existing filtered environment without this variable,
because5.8.2 also interprets an inherited variable as the conflicting option.
This is an explicit correction to the literal “plus” in the ruling, for Kai's
adjudication and Vera's review. No alternative clock mode is silently enabled.

The change does not repair embedded timestamp-mode pyc headers by itself.
Cox's separately reviewed Dockerfile change owns those. Controller tests do not
claim native compatibility or artifact reproducibility; no OCI output is
rewritten, normalized or relabelled after production.
