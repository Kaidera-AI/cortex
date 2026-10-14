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
