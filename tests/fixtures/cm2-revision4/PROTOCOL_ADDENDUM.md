# CM-2 readiness helper protocol — R216

Authority: /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-06_kai_rulings-r216.md. Confirms the retained outer/inner manifests and read-only challenge protocol proposed by ren-kos. The signed-release executable is **bin/cortex**. Its planned command is:

```sh
"$package_root/bin/cortex" prerequisite-proof --descriptor "$descriptor_path" --nonce "$nonce"
```

`package_root` and `descriptor_path` are the exact absolute, physical, same-user private paths selected by the descriptor. `nonce` is a fresh 16-byte random challenge encoded as exactly 32 lowercase hexadecimal characters. The operation reads the named installation; its observations and database transactions are bounded and read-only. It never provisions a project, issues keys or mutates the engine.

## Retained bytes

| Artifact | Exact retention location |
| --- | --- |
| Signed external cortex.release.v2 | runtime_root/signed/release.json |
| Detached Minisign signature | runtime_root/signed/release.json.minisig |
| Exact downloaded archive | runtime_root/signed/ plus the validated basename in signed archive.name |
| Extracted package | descriptor.package_root; the installed runtime uses runtime_root/package |
| Inner package manifest | package_root/release.json |
| Signed executable | package_root/bin/cortex |

The signed external manifest binds the retained archive's size/digest, exact inner-manifest digest and `files["bin/cortex"]`. There is no checksum cycle: the external manifest/signature are retained beside the archive, outside its payload. The consumer verifies the exact helper bytes before invocation, independently of any self-reported Boolean, and binds the measured digest to proof.helper_sha256. The independently pinned release key is `7BBB4B7E0CE8852E`; the descriptor supplies no trust. The public half is /Users/amadmalik/DevVault/helix/output/release-keys-2026-10-06/kaidera-release.pub.

## Exit and bounds

- **0:** one strict, duplicate-free UTF-8 cortex.prerequisite-proof.v2 JSON object on stdout, with one final newline, at most **65,536 bytes including that newline**. Echo the exact nonce and bind the descriptor/release/helper digests. Stderr is empty. Success requires all fresh native observations; cached install receipts do not qualify it.
- **2:** a typed refusal or invalid input. Stdout is empty; stderr contains one safe JSON object, at most **4,096 bytes** including its newline: code, safe_message, install_guide, utc, and http_status_if_observed only when present. No credential values, hashes/fingerprints, raw auth body or database material.
- **Any other status, signal, timeout, oversized or malformed stream:** consumer refuses; it never treats partial output as a proof or retries provisioning.
- Helper wall deadline: **30 seconds**; every engine command/database observation is at most **5 seconds** and shares that deadline. This is included in the existing **60-second** overall consumer admission. The subsequent three member HTTP calls remain at most5 seconds/65,536 bytes each, within that same overall deadline.

## Freeze disposition

Revision3's 28 previously frozen files and its add56978 freeze remain unchanged. Its synthetic release fixtures name bin/cortex-test, so the release-name change requires **revision4**, not an operator alias or reinterpretation of those bytes. Revision4 changes that helper binding and its transitive descriptor/proof digests, adds these explicit protocol bounds, and preserves all147 vector IDs/outcomes/phases and all94 baseline decisions. The full adopt/reissue/Mac vectors remain frozen, with implementation deferred under R210. This addendum describes the future signed release; it does not claim that such a binary or signed bundle exists yet.
