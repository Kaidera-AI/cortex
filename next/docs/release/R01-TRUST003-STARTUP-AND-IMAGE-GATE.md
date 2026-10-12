# TRUST-003 repair: isolated startup and closed image admission

Authority: Kai 2026-10-12 11:27b. This is the named bounded repair to PR140's accepted plan, not a native executor GO.

The inspector portion of release-policy.v1 is narrowed: literal argv `/opt/cortex-inspector/bin/python -I -S -B /opt/cortex-inspector/inspect.py`, optimization integer 0 only, empty environment, no hash_seed field. The startup policy claims isolated mode, ignored Python environment, no site and no bytecode writes; it makes no deterministic hash-seed claim. Build/compiler determinism fields are separate and unchanged. `startup_argv` enforces this boundary and prepare_plan consumes it. Positive/negative controls use a known local interpreter only and verify actual flags under conflicting ambient PYTHON variables; no candidate code/container executes.

Closure admission cannot verify executed image/member/path/ABI identity today. A hash-accepted local manifest, local members and image digest string are insufficient. Until a separately accepted verified-image port exists, admit_closure refuses inspector_image_member_unbound before decode, physical member reads or minting a sealed capability/plan. No caller-set accepted flag, digest, fixture closure or typed identity can bypass this gate. The unconditional native_inspector_execution_unqualified refusal remains.

Vera's original RED.py/JSON are retained unchanged as historical evidence. Their inert positive and ineffective-settings test cannot become green unchanged under the intentionally narrowed/closed policy: they request optimization2/hash_seed and admit an unverified image. Versioned startup controls decide the new allowed policy; the original image-refusal desired outcome now occurs before plan admission. This does not grant native inspection or containment.

Named Podman engine, supervised bounded transport/time/cancel, custody through launch and actual contained positives/negatives remain R02b/F01 executor gates, outside this repair. No image build/run/pull or host change.
