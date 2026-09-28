DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'coordination fix migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

-- ---------------------------------------------------------------------------
-- Forward-only corrections to 0003_coordination.sql (already applied; its
-- bytes and checksum are frozen). Two changes:
--
-- 1. The handoff update trigger's transition matrix omitted two legal
--    withdrawals that the domain state machine allows: open -> withdrawn and
--    rework -> withdrawn. Replace the function body with the full matrix;
--    every other rule (immutability, revision CAS, claim generation
--    monotonicity, lease/reclaim semantics, policy recheck) is unchanged.
--
-- 2. Structural relay-target integrity (residual review P9): approvals,
--    relay authorizations and relay receipts carried target_actor_id without
--    the same-scope composite membership FK that handoffs.addressed_actor_id
--    and task_assignments already enforce. Add it; NULL target pairs (every
--    non-relay approval) pass the composite FK unchanged.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION cortex_coord.validate_handoff_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    legal boolean := false;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.handoff_id <> OLD.handoff_id
       OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key
       OR NEW.title <> OLD.title
       OR NEW.brief <> OLD.brief
       OR NEW.addressed_role IS DISTINCT FROM OLD.addressed_role
       OR NEW.addressed_actor_id IS DISTINCT FROM OLD.addressed_actor_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.require_human_accept <> OLD.require_human_accept
       OR NEW.created_by_principal <> OLD.created_by_principal
       OR NEW.created_at <> OLD.created_at
       OR NEW.relay_id IS DISTINCT FROM OLD.relay_id
       OR NEW.relay_source_scope_id IS DISTINCT FROM OLD.relay_source_scope_id
       OR NEW.relay_source_handoff_id IS DISTINCT FROM OLD.relay_source_handoff_id THEN
        RAISE EXCEPTION 'handoff identity and acceptance rules are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'handoff revision must advance by exactly one'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.claim_generation < OLD.claim_generation THEN
        RAISE EXCEPTION 'claim generation is monotonically increasing'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status IN ('accepted', 'abandoned', 'withdrawn') THEN
        RAISE EXCEPTION 'terminal handoffs cannot change' USING ERRCODE = '23514';
    END IF;

    IF NEW.status = 'claimed' THEN
        IF NEW.active_lease_expires_at IS NULL
           OR NEW.active_lease_expires_at <= now() THEN
            RAISE EXCEPTION 'a claim requires a future lease'
                USING ERRCODE = '23514';
        END IF;
        IF OLD.status = 'claimed' THEN
            IF NEW.claim_generation = OLD.claim_generation THEN
                -- Explicit lease renewal of the active claim.
                IF OLD.active_lease_expires_at <= now() THEN
                    RAISE EXCEPTION 'an expired lease cannot be renewed'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.active_lease_expires_at <= OLD.active_lease_expires_at THEN
                    RAISE EXCEPTION 'lease renewal must extend the lease'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.claim_generation = OLD.claim_generation + 1 THEN
                -- Reclaim strictly after lease expiry (fencing advances).
                IF OLD.active_lease_expires_at > now() THEN
                    RAISE EXCEPTION 'a live claim cannot be reclaimed'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'claim generation must advance by exactly one per claim'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF OLD.status IN ('open', 'rework') THEN
            IF NEW.claim_generation <> OLD.claim_generation + 1 THEN
                RAISE EXCEPTION 'claim generation must advance by exactly one per claim'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'handoffs are claimed only from open or rework'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.active_lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'only claimed handoffs carry an active lease'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.claim_generation <> OLD.claim_generation THEN
            RAISE EXCEPTION 'claim generation changes only through claims'
                USING ERRCODE = '23514';
        END IF;
        legal := (OLD.status = 'claimed'
                  AND NEW.status IN ('open', 'returned', 'failed',
                                     'abandoned', 'withdrawn'))
              OR (OLD.status = 'returned'
                  AND NEW.status IN ('accepted', 'rework', 'withdrawn'))
              OR (OLD.status = 'rework' AND NEW.status = 'withdrawn')
              OR (OLD.status = 'failed'
                  AND NEW.status IN ('open', 'withdrawn'))
              OR (OLD.status = 'open' AND NEW.status = 'withdrawn');
        IF NOT legal THEN
            RAISE EXCEPTION 'handoff transition % -> % is not allowed',
                OLD.status, NEW.status
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION cortex_coord.validate_handoff_update() FROM PUBLIC;

ALTER TABLE cortex_coord.approvals
    ADD CONSTRAINT approvals_relay_target_membership_fk
    FOREIGN KEY (relay_target_scope_id, relay_target_actor_id)
    REFERENCES cortex_auth.memberships(scope_id, actor_id)
    ON DELETE RESTRICT;

ALTER TABLE cortex_coord.relay_authorizations
    ADD CONSTRAINT relay_authorizations_target_membership_fk
    FOREIGN KEY (target_scope_id, target_actor_id)
    REFERENCES cortex_auth.memberships(scope_id, actor_id)
    ON DELETE RESTRICT;

ALTER TABLE cortex_coord.relay_receipts
    ADD CONSTRAINT relay_receipts_target_membership_fk
    FOREIGN KEY (target_scope_id, target_actor_id)
    REFERENCES cortex_auth.memberships(scope_id, actor_id)
    ON DELETE RESTRICT;
