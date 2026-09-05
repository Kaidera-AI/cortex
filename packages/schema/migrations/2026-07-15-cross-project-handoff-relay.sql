-- Preserve an approved cross-project handoff sender without registering that
-- sender in the destination roster. All handoffs without the signed relay
-- evidence continue to normalize every identity into the row's project.

CREATE OR REPLACE FUNCTION public.cortex_identity_v2_normalize_handoff_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
DECLARE
    cp record;
    source_cp record;
    slug text;
    relay jsonb;
    relay_source_project text;
    relay_target_project text;
    relay_source_agent text;
    relay_target_agent text;
    relay_approval_id text;
BEGIN
    IF NEW.project IS NULL OR lower(btrim(NEW.project)) IN ('_global', '_local_state') THEN
        RETURN NEW;
    END IF;

    SELECT id, project_key
      INTO cp
      FROM cortex_projects
     WHERE project_key = lower(btrim(NEW.project));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cortex project % is not registered', NEW.project
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    NEW.project_id := cp.id;

    IF COALESCE(NEW.evidence, '{}'::jsonb) ? 'cross_project_relay' THEN
        relay := NEW.evidence->'cross_project_relay';
        IF jsonb_typeof(relay) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'cross_project_relay evidence must be an object'
                USING ERRCODE = 'check_violation';
        END IF;

        relay_source_project := lower(btrim(COALESCE(relay->>'source_project', '')));
        relay_target_project := lower(btrim(COALESCE(relay->>'target_project', '')));
        relay_source_agent := lower(btrim(COALESCE(relay->>'source_agent', '')));
        relay_target_agent := lower(btrim(COALESCE(relay->>'target_agent', '')));
        relay_approval_id := lower(btrim(COALESCE(relay->>'approval_decision_id', '')));

        IF COALESCE(relay->>'schema_version', '') <> '1'
           OR relay_source_project = ''
           OR relay_source_project = cp.project_key
           OR relay_target_project <> cp.project_key
           OR relay_approval_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid cross_project_relay boundary for project %', NEW.project
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT id, project_key
          INTO source_cp
          FROM cortex_projects
         WHERE project_key = relay_source_project;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Cortex relay source project % is not registered', relay_source_project
                USING ERRCODE = 'foreign_key_violation';
        END IF;

        slug := cortex_identity_base(NEW.from_agent);
        IF NOT cortex_identity_v2_valid_slug(slug)
           OR relay_source_agent <> cortex_identity_display(slug, source_cp.project_key) THEN
            RAISE EXCEPTION 'Invalid Cortex relay from_agent % for source project %', NEW.from_agent, source_cp.project_key
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.from_actor_id := cortex_identity_v2_ensure_actor(
            source_cp.project_key,
            NEW.from_agent,
            TG_TABLE_NAME || '.cross_project_from_agent'
        );
        NEW.from_agent := cortex_identity_display(slug, source_cp.project_key);
    ELSE
        slug := cortex_identity_base(NEW.from_agent);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex handoff from_agent % for project %', NEW.from_agent, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.from_actor_id := cortex_identity_v2_ensure_actor(
            cp.project_key,
            NEW.from_agent,
            TG_TABLE_NAME || '.from_agent'
        );
        NEW.from_agent := cortex_identity_display(slug, cp.project_key);
    END IF;

    IF NEW.to_agent IS NOT NULL AND btrim(NEW.to_agent) <> '' THEN
        slug := cortex_identity_base(NEW.to_agent);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex handoff to_agent % for project %', NEW.to_agent, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        IF relay IS NOT NULL
           AND relay_target_agent <> cortex_identity_display(slug, cp.project_key) THEN
            RAISE EXCEPTION 'Invalid Cortex relay target_agent % for project %', NEW.to_agent, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.to_actor_id := cortex_identity_v2_ensure_actor(
            cp.project_key,
            NEW.to_agent,
            TG_TABLE_NAME || '.to_agent'
        );
        NEW.to_agent := cortex_identity_display(slug, cp.project_key);
    ELSE
        IF relay IS NOT NULL THEN
            RAISE EXCEPTION 'Cross-project relay requires a destination agent'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.to_actor_id := NULL;
    END IF;

    IF NEW.claimed_by IS NOT NULL AND btrim(NEW.claimed_by) <> '' THEN
        slug := cortex_identity_base(NEW.claimed_by);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex handoff claimed_by % for project %', NEW.claimed_by, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.claimed_by_actor_id := cortex_identity_v2_ensure_actor(
            cp.project_key,
            NEW.claimed_by,
            TG_TABLE_NAME || '.claimed_by'
        );
        NEW.claimed_by := cortex_identity_display(slug, cp.project_key);
    ELSE
        NEW.claimed_by_actor_id := NULL;
    END IF;

    RETURN NEW;
END;
$$;

