# R221 bounded CI publication rework

Authority: H-D287 in Kai's R221; current R227 retains the same repair and gates. Preserve the original bedf44bf3e4d63b876d1d63823517e426d9c4df8 author checkpoint and its REWORK evidence.

1. Add `test_linux_ci_environment_publication.py` only. Freeze all 114 inherited test/helper files. Confirm the FIFO and unterminated predecessor failures before product edits; positive empty/terminated files remain literal. Run the unchanged 61 inherited selected source suites plus the entire additive suite.
2. Change only the environment publisher: use a nonblocking, no-follow read/write open; verify the regular owned single-link inode as before; inspect at most the final byte and refuse an unterminated predecessor before writing. Keep storage fields, workflows, recipes, dispatch guard and runtime pins unchanged.
3. Repeat the same 62 complete suites, then inventory collected test files against every `test_*.py` module as required by R227. Run every portable module, reporting environment-gated skips and these five native-only exclusions by name: `test_sandbox_api.py`, `test_w1_candidate.py`, `test_ops_candidate.py`, `test_mount_readiness_candidate.py` (require an admitted candidate API/database; several perform network calls at import), and `test_member_key_reader.py` (actual native Mac/Linux key custody; never the CTO login keychain). Helper-only modules with no test cases remain named in the inventory. No silent omissions or full-repository GREEN claim.
4. A fresh final author check, exact source push/PR/origin readback and Vera follow. A new native rehearsal still requires Vera ACCEPT and Kai's new full-SHA admission. No native retry, signing, product install, foundation restart or inherited-store mutation.

The five native modules remain release/runtime obligations. This source rework cannot qualify a native package, actual Podman or installed callers.
