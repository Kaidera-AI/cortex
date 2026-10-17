# Finite seed

`seed_runtime_bindings(config, operation_key, deadline)` runs only inside Core's
actual `open_seed_resources` context, with the same immutable config object and
original absolute deadline. It returns the exact eight-field receipt after the
encrypted client record durably acknowledges COMMITTED. Core's operator closes
the SQL connections and independently reloads that receipt before printing success.

The custody lease spans the whole operation. Core's catalog accessor checks the
complete existing migration ledger and calls the sole packaged ship validator.
The bootstrap transaction takes the existing migration advisory lock, locks the
binding map before installation anchors, rechecks the schema, and compares the
exact SQL anchors, three bindings and any existing enrollment receipt. A missing
private record with existing or partial SQL refuses. Only serialized absence can
mint the two identity UUIDs and one 32-byte random token, encode it as 64 ASCII
hex bytes, and durably encrypt PREPARED before any SQL write.

Anchors and bindings commit together. The actual installer login then calls the
existing `auth.first_identity`; its exact committed SQL receipt is read back.
The API login explicitly sets its request group and calls existing enrollment
readiness. A fresh policy/schema observation precedes the COMMITTED checkpoint.
Every replay compares the complete private intent and original operation UUID,
uses the original IDs/token, and rechecks current authority and readiness. Failed
or ambiguous acknowledgements stop the operation; the next invocation reloads
and reconciles. No path rotates credentials, repairs differing rows, provisions
or reopens finite roles, applies migrations, or implements enrollment SQL itself.

The source tests compose actual Core custody/resources/operator, the packaged
sole role validator and existing helper with explicit database and loopback HTTP
fixtures. They do not qualify real PostgreSQL concurrency, native TLS, installed
closure or a served application. Those require independently authorized receipts.

Dependency pins and exact execution history belong to the owner handoff evidence,
not this usage contract. Shared SQL inventory pins/admission are owned by Mike.
