# S1 make_secret: interface, the private lifetime join, and what stays open (day 3 correction, RED only)
Source: this directory, `make_secret.py` (signature and full contract in its docstring), tests `tests/test_make_secret.py`. No implementation, no secret, no key, no custody effect. Order A (cox):
nonsecret `write_config` first, then ONE finite bootstrap, five imports, the SAME bootstrap bytes to the fixed S4 receiver, then the sequence.

```
write_config (S2)
  -> render.finite_bootstrap(config_path, plan=, unlock_fd=, key_id=, operation_key=, record=, engine=, deadline=D)    S4, yields the ACTUAL Core InstallerCustody (IMPORT)
       -> make_secret(owner, producer=<J06>, deadline=D)                                                                 S1, the SAME D
            admit -> produce x5 (admit after each) -> validate -> admit+import x5 -> admit -> render.receive_bootstrap(owner, bytes) -> admit
       -> S4: its own admission, owner.run(operation_key) once, reverse cleanup, custody exit, completion object
  any S1 refusal -> core._installer_fail(owner) first -> exception leaves the `with` -> S4 unwinds (or, if caught inside, S4 admission and owner.run refuse)
```

**Frozen, reused:** five purposes and import order `bootstrap, migrator, api_request, probe, installer_enrollment` (`roles.A_PURPOSES`; Core `ROLE_NAMES` is the same set); `owner.import_credential(purpose, bytes)`;
`roles.MIN_SECRET`; one config, one deadline, one owner. **Not chosen:** the J06 producer of the five credentials and of the unlock key (an explicit argument, no default).

**Changed by this correction (the only contract change):** the arbitrary `bootstrap_sink` callable is GONE. S1 resolves the fixed S4 receiver `render.receive_bootstrap(owner, bootstrap)` through `_receiver()`.
`bootstrap_sink_failed` is retired for `bootstrap_receiver_failed`. The caller-supplied `deadline` must equal the owner's own original deadline (`deadline_not_original`); it never replaces it.

## HALE-S1-LIFETIME-002: the existing private Core admission and failure path, named
Core functions used (reference: selected Core 90daf7ea, `custody.py` blob 3c971310; they are NOT at this base, so `_core()` is unbound and the call refuses):
 - `_current_installer(config, custody, deadline)` (lines 328-360): returns the bound owner only if the originating context still holds it, exact type, the original config/vault/deadline object, not closed,
   not tainted, stage IMPORT/RUN/phase, unexpired. S1 calls it with the owner's OWN slots `(owner._config, owner._vault, owner._deadline)` and requires the result `is owner`.
 - `_installer_fail(owner)` (line 444): taints the owner and sets stage ERROR. S1 calls it before raising.
 - Slots read, never written: `_stage == 'IMPORT'`, not `_busy`, not `_tainted`, not `_closed`, `_imported`. `_imported` must equal what S1 itself imported so far, so a callback that imports, runs or lies is seen.
 - S4 (reference: head dd9c2f80, render.py blob 7e94cc47): `finite_bootstrap` yields the owner; `receive_bootstrap(owner, bytes)` admits before and after its engine boundary and taints on any failure;
   after the block S4's `_admit_owner` demands stage IMPORT, not busy, not tainted, `_imported == ROLE_NAMES`, then `owner.run`. S1's checks around the producer and the five imports are NOT replaced by it.

| boundary | S1 requirement | refusal | owner |
|---|---|---|---|
| entry | exact owner type; Core and receiver resolved; producer defined; deadline valid/unexpired; not re-entered | custody_owner_unbound / custody_owner_type / bootstrap_receiver_unbound / j06_producer_undefined / deadline_invalid / deadline_expired / make_secret_reentered | untouched (reentry fails the ACTIVE owner) |
| admission before the producer | Core admission, fresh IMPORT, nothing imported, deadline equals the owner's original | custody_context / deadline_expired / deadline_not_original | failed |
| after EVERY producer return | same admission again (closed, expired, stage moved, import made, tainted, reentered) | custody_context / deadline_expired | failed |
| validate | five bytes, length >= MIN_SECRET, pairwise distinct | secret_invalid:<purpose> / secret_reused | failed |
| before each import and after the fifth | same admission with `_imported` = what S1 imported so far | custody_context / deadline_expired | failed |
| the five imports | owner.import_credential in the frozen order | import_failed:<purpose> (callback failure: producer_failed:<purpose> for the producer) | failed |
| the fixed receiver | called once with (owner, the imported bootstrap bytes), only after the admission with all five held | bootstrap_receiver_failed | failed |
| after the receiver | same admission with all five held; success only after it | custody_context / deadline_expired | failed |
| any BaseException from a callback | fail the owner, then propagate | (the exception) | failed |

Reentry marker: a call-scoped ContextVar holding the active owner (the device S4 uses for `_BOOTSTRAP`); nothing survives the call. No flag, phase setter, store, secret keeper, default or caller `verified` value exists.
The owner is a trusted-code composition (the same trust boundary as Core and S4), not a hostile-process isolation.

## What this does NOT establish
Actual Core and actual S4 are not at this base: every case in `tests/test_make_secret.py` runs over NAMED DOUBLES that model the conditions above, and the join to the actual classes is a later, separately
commissioned receipt. The J06 producer, unlock descriptor, initdb transfer, signing, install view and native controls are NOT RUN. make_secret is now IMPLEMENTED at component level (J1, row feb9dc8b): the 55 closed tests pass unchanged and `tests/test_make_secret_effect.py` adds 5 for S1's own reading of the owner slots; the three resolvers stay unbound, so production still refuses `custody_owner_unbound` until the Core and S4 join lands.

## Open join choices (for Cox, not made here)
 1. The producer stays a caller-supplied object (the J06 seam). If "no caller-supplied callback" is meant to include it, J06 must first fix how the five values arrive; that is the held human decision.
 2. Reentry marker: a call-scoped ContextVar (recommended; S4 uses the same device). Without it a nested call can complete a second production and delivery before the outer call notices.
