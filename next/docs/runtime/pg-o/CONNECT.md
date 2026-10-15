# CONNECT version 2

Kai 6e34eb90 folds Cox's accepted narrow CONNECT ruling into repair 0368a245.
The bound matrix is exactly e2bcec216f4b90d92c8e53a73ae202d265f2def411a4a744469c42a22164e3cd.
The prior matrix is retained verbatim as ratified_matrix_v1.json. There is one
capability change: verifier_owner gains CONNECT on the owner-admitted current DB.
TEMP, NOLOGIN, every closed flag/edge, no CREATE, no grant options and no stats
membership remain. No explicit other-database grants are admitted. Foreign PUBLIC
database ACLs are outside this migration's ownership; a separately created closed
database decides effective denial and pg_database_size native42501.

All 175 prior node bodies are byte-identical to the repaired oracle. The existing
test_grants.Grants.test_other_objects__verifier_owner is explicitly versioned:
its unchanged matrix-driven CONNECT expectation changes False to True. No old
failure is hidden/deleted. Six new test_connect.Connect methods decide current DB
capabilities, positive/revoked/restored size, flags/all edges, real NOLOGIN,
explicit foreign ACL absence, and closed foreign DB CONNECT/size denial.

Tests/controls were committed before matrix renewal. With the old matrix, the
current CONNECT and size positives fail in BODY. With the accepted matrix, all70
static controls (60 retained plus10 CONNECT) pass their golden/restoration cases
and kill their intended wrong-row outcomes; 21 original families remain killed.
No SQL actually executes in these controls. NOLOGIN's static connection outcome
does not claim an authenticated native session; its frozen future probe is real.
The native login check also requires the specific NOLOGIN error text, so unrelated
transport failures cannot earn refusal credit.

CONNECT_FREEZE.json supersedes the historical REPAIR_FREEZE.json for current
181-node renewal. Original105 historical42/46/17 receipts stay unrenewed. Cox must
run ALL181 against the ratified tuple and original owner material; Hale reviews
repair plus this CONNECT delta together. No product source/schema or host changed.
