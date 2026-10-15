# E2 ratified declaration composition

Cox ruling A, `docs/handoffs/2026-10-14_cox_kai_e2-lock-ruling.md`, supersedes the
old unchanged-lock-schema instruction. The exact ratified three-file amendment
SHA256 `547bd3fe193ec88ddd2f5a55c01234b36597924a929d10395d9c8ba3579ce58f` is now
applied to this branch. The lock-schema digest is
`82cc5af6ab758e8fc51ada65e3cb8ec574f6e7bf8960802849c58280b07463aa`; the fixed
loader pin matches, and all fourteen synthetic fixture config references bind
lock digest `c2d71dcc5975c6c62571972886b1924e7807b21a3f674ba8778ee524643c7bb5`.
The exact config/operations declarations and all original test bytes are retained.
Admission supports one current exact lock; there is no old/new fallback or bypass.

Actual-branch verification and guard replay receipts for this source completion
live in `output/Cortex/nemo-e2-complete-20261014/`; the return is
`docs/handoffs/2026-10-14_nemo_kai_e2-complete.md`. Receipt source hashes and Git
head bind each fresh run to the actual branch. This revision is subject to Mike's
source review and the existing original owner/Core/C04/RC3/native gates.

E1 remains RC3b-gated: hosted model/origin/redirect binding, absolute cache deadline
propagation, and OFF late cache publication/release fencing. The released KOS
results-only search method's partial503 masking remains with its consumer owner.
No provider, key, spend, PostgreSQL or lab run belongs to this completion.

LEAN: reused the ratified schema/pin/fixture admission and existing E2 mechanisms;
did NOT add a parser, verifier, port, config path or compatibility lock;
could delete or merge none without removing required behavior.

## Historical return before Cox lock ratification

The following preserves the earlier partial-return context and its explicitly
unapplied-proposal evidence. Its branch counts and declaration blocker describe
that earlier revision; current actual-branch counts are in the completion return.

# E2 source and fixture return

Disposition: PARTIAL. Canonical text and bounded OFF keyword paths are implemented;
the accepted declaration set contradicts itself, so strict branch admission stays
closed. This is source and fixture evidence, with no native/effect qualification.

The exact Cox patch is standalone commit `b9d0d45271db889698d5b01386e8c5753da87d75`,
after normal main merge `25ce517aad0ceba07d65b5303a2fd215612d5ef9`. It changes the
operations digest to `51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b`.
The unchanged lock schema still requires the prior operations digest
`32eb25a99a6fdf94f770f5d6b1584ff4b58801686f96a57ad6259788ec8b94a0`.
Both cannot hold. The lock-schema pin has been preserved as instructed; validation
has not been relaxed. Cox must ratify a coherent schema/pin/fixture composition.

Implementation reuses the existing strict decoder, current C04 connection and
scope, C11 search/target/graph ports, ASGI gateway and consumer fixture validator.
Memory is `section + newline + content`; session messages retain canonical role,
content and order. Metadata is excluded. Unknown/binary, malformed and oversized
records remain typed unsupported. Reads scan at most 100 records plus one overflow
witness, use literal Unicode casefold matching and deterministic ID ties, and
revalidate authorization and canonical revisions/payload hashes. Complete reads
emit 200 ready/ready_empty; incomplete reads emit 503 partial with typed unsupported
IDs and canonical freshness. Reranking makes zero provider calls. One absolute
request budget is capped at ten seconds. Separate graph read paths remain intact.

Evidence under `output/Cortex/nemo-e2-keyword-20261014/` distinguishes the requested
branch from the strict, **unapplied and unratified** owner-amendment copy:

| Gate, each on Python 3.14.0 and 3.13.7 | Requested branch | Unapplied owner proposal |
| --- | --- | --- |
| Frozen E2 subset (baseline 1 pass, 16 BODY RED) | 15/17 pass; two declaration RED | 17/17 pass |
| Added E2 methods | 44/47 pass; three declaration methods RED | 47/47 pass |
| Inherited RC0/runtime/identity/factory/provider | 87/148 pass; declaration failures/errors retained | 148/148 pass |
| Existing C11a gateway | 14/14 pass | Same source |
| Existing consumer contract | 39/39 pass | Same source |
| Existing release input guards | 5/5 pass | Same source |

All six frozen E0 files, all 273 inherited files under `next/tests`, and the
original RC0 test code remain byte-identical. Prior RC0 fixture bytes are retained
in evidence. The proposal has 79 distinct source-guard controls, replayed on both
interpreters: baseline green, intended BODY assertion kill, exact restoration and
green, with zero errors or attempted real effects. It changes only the lock-schema
operations const, its fixed loader pin and the corresponding synthetic lock
descriptor/digest. This does not authorize applying it to the branch.

Released-client checks execute the original pinned KOS search method against the
actual in-process ASGI gateway, and type-erased pinned OpenKai client/dependencies
against its response bytes through injected fetch. Complete hits and empty reads
are preserved. OpenKai throws typed HTTP 503 errors retaining partial bodies; KOS
catches HTTP errors and returns `[]`, losing the partial-versus-empty distinction.
KOS needs a separate owner change. These checks grant no native SDK/authentication
qualification and use synthetic identities/current records only.

E1 remains: admitted hosted model/origin/redirect enforcement before key or HTTP,
one absolute deadline propagated through the real query cache, and OFF cancellation
plus late cache publication/release fencing through original RC3/native bindings.
The whole frozen E0 oracle in the proposal is 41/46 pass, with exactly those five
E1 assertions still RED. No PostgreSQL, lab, live provider, key or spend was used.

LEAN: reused decoder, C04 reads, C11 gateway/target/graph and contract validator;
did NOT add a parser, index/store, SQL role/writer, queue or provider binding;
could delete or merge none without removing a required boundary.
