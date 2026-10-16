# X4 partial freeze — CTO H-D576 FOCUS

Authority: Kai row fc3dc25c-c627-4711-b153-e5b328e5e69b; accepted immutable X4 design 31295690b3a724c3fdab32b60540173300a445a8b9de2cf22c3d26c2772179c0. Kai 14:30 orders commit/push and PARTIAL return before Linux FOCUS planning. No more X4 implementation in this row.

Plan followed: freeze original-history phase tests before implementation; reuse P1 lifecycle, existing migration/checksum engine and finite role adapter. Add private install/core_sql.py, extend migrations.py IO/commit/checkpoint hooks and role_grants.py exact finite admission/demotion/migration seams. Do not edit historical SQL, use native effects or wire runtime startup.

Refinements: phase effects have distinct original effect IDs; source fixture uses original sealed20 selection and controlled local bridge clock while retaining real P1 filesystem operations. The private adapter compares selection with the existing R01 original member, preserves failed nonsecret unresolved inventory and carries a finite IO clamp through the existing SQL client. All native observations in these tests are DATA doubles.

Stop boundary: source fixture work only, not merge-ready. Remaining verification: full final inherited-regression comparison, changed-mechanism negative controls, independent Hale/Vera review, installed/native qualification. Known design/implementation concerns for resumed review: SQL set_config SELECT before SET TRANSACTION isolation; recovery forwarding versus the existing fixed-owner-inferred measure interface; finite M/I guard through terminal publication/ACK; ordinary restart fresh authority versus historical windows. These concerns are OPEN, not accepted or silently deferred as completed work. Finn X1-c must not consume this tip as qualified/complete.

No PR, merge or deploy is authorized by this partial freeze. Native suppliers/secret custody and X1-c remain separate and unbound.
