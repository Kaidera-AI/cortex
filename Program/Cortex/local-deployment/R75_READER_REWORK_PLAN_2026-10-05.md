# R75 reader repair execution plan

Owner: ren-cx@helix. Accepted scope: Kai's r75 ruling, 2026-10-05 01:24, specifically OCR-001 and OCR-002, RED first, isolated native Mac verification, new SHA and frozen archive. This records execution within that ordered repair; source acceptance remains Vera's gate.

Authority: /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_kai_rulings-r75.md.
Review: /Users/amadmalik/DevVault/helix/Program/ReviewService/reviews/2026-10-05-vera-reader-dfe12cdea3.md.

## Pre-edit evidence and boundaries

- Clean starting source: dfe12cdea374649786cbe2636a0aeacad074168a, origin's ren-cx/r65-key-reader-20261004.
- Owned locked worktree: /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r75-reader-rework-20261005; branch ren-cx/r75-reader-rework-20261005. Verified SSD UUID 8071CB26-B817-496E-A1FC-A8FE9A87C184.
- Host headroom: memory_pressure reports 48% free; Podman VM approximately 57% available. No VM reconfiguration or concurrent heavy build.
- Native Mac mutation only in GitHub's ephemeral macos-14 runner. Local Mac tests replace Security.framework with a fake; no CTO keychain calls or fixture writes to the SSD. Linux private fixture state lives in ephemeral /tmp tmpfs. Public worktrees, test sources and downloads may use SSD.
- Existing TEST package, reader archive, KOS pin and R70 bootstrap source are immutable or separately owned. No release, installation, admission, live conversion, merge, tag or Cortex queue write.

## Files and order

1. Commit this plan and frozen regression tests before implementation: tests/test_key_store_r75.py, scripts/release/check-reader-r75-native.py and .github/workflows/cortex-reader-r75.yml.
2. Record baseline failures for initial root/installation absence, missing deletion, CLI exit/output, unsafe custody, enumeration disappearance, injected native-store invalidation, item custody and false post-check. Fake APIs are explicitly labelled as synthetic evidence.
3. Repair only src/cortex_v2/clients/key_store.py and src/cortex_v2/clients/mac_keychain.py. Linux absence may return empty only during the initial private-directory validation; late descriptor/enumeration failures remain errors. Missing delete is idempotent.
4. Mac: create a floating item with SecKeychainItemCreateNew; set service/account attributes; call SecKeychainItemAddNoUI with a mandatory non-null explicit handle. Verify item keychain with SecKeychainItemCopyKeychain, CFEqual and SecKeychainGetPath. Refuse false final custody checks. Balance native references and never include native buffers in exceptions.
5. Run the same frozen checks after repair. Hosted Mac fixture supplies synthetic default/search-list keychains, deletes the dedicated handle immediately before native add, and checks both ambient stores remain empty. Exercise normal read, rotation, lock/unlock and deletion from frozen zip bytes. Preserve failed attempts.
6. Freeze a clean source SHA, build deterministic new zip and inventory, verify identical hosted zip digest, push the review branch and verify origin. Return exact range, test identities, archive SHA256, native receipts and limits in /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_ren-cx_kai_r75-reader-rework.md. Publish a new dated contract, leaving the old contract/archive intact.

## Source decision and risks

Pinned Apple Security source db15acbe6a7f257a859ad9a3bb86097bfe0679d9 shows that SecKeychainAddGenericPassword, SecKeychainItemCreateFromContent and SecItemAdd's classic path can select defaultKeychainUI. SecKeychainItemAddNoUI resolves a non-null handle through KeychainImpl::required and adds to that same KeychainImpl, without ambient-store selection. The floating-item symbols are exported by this Mac SDK but declared in Apple's private deprecated header: portability is explicitly bounded by native qualification and fail-closed symbol loading. No claim of long-term SPI stability or signed-release qualification.

Primary source URLs:
- https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecKeychainItem.cpp
- https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/Keychains.cpp
- https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecItem.cpp

Rollback: retain previous frozen bytes and withdraw this new review input if qualification fails. Do not change any installed consumer. Native invalidation is controlled failure injection, not a reproduction claim about the CTO's Mac. Vera reviews the new exact tip; Kai rules on use and consumer re-pin separately.
