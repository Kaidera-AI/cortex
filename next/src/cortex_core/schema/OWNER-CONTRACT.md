# Seed owner contract correction — Cox retry4 Option B

The strict Core config has no expected database-owner field. The sole packaged
Nix policy refuses service-owned databases, while a fresh empty database may
have an otherwise safe non-service administrator as owner. It becomes part of
the durable observed seed intent. PREPARED and COMMITTED replays must refuse a
changed observed owner before further SQL writes or custody replacement.

Cox explicitly ruled this correction in
2026-10-17_cox_finn_a-day2-rework4.md (SHA256
c2ca9b6d8cdb336efae7ee4738fbf74ca12ea7bc5df9621b3af20b7b31e521d1).
The original test_unsafe_flags_foreign_membership_and_database_drift_refuse
used fresh owner999, despite having no previously pinned owner. Its sole
database-owner input now uses the configured api_request service OID. Every
other assertion in the original25 remains unchanged. This corrects the fresh
input under the actual sole policy; it does not weaken owner replay checks.

The original test bytes and RED logs remain in immutable retry3 evidence
(seed test SHA2564a7c900126de397cb2b349254d5fd4cc07468529c1860832bea2cb68ddb89541).
New test_seed_owner_contract.py explicitly covers PREPARED owner drift after
a lost durable acknowledgement before first SQL write, COMMITTED owner drift,
and a fresh non-service administrator becoming durably pinned. Its unchanged
assertions also run against a source mutation that ignores observed-owner drift.
No fixture supplies real PostgreSQL/concurrency/installed proof. No production
policy is duplicated or changed, and the sole seed source remains unchanged.
