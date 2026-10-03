-- Keep the canonical project/actor foreign keys on agent rows synchronized.
-- ServiceAuth resolves only exact canonical identities; text-only agent inserts
-- must not create principals detached from their project or actor records.

CREATE OR REPLACE FUNCTION public.cortex_agents_sync_identity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public
AS $$
DECLARE
    canonical_project record;
    canonical_actor uuid;
BEGIN
    SELECT id, project_key
      INTO canonical_project
      FROM public.cortex_projects
     WHERE project_key = lower(btrim(NEW.project));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cortex project % is not registered', NEW.project
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NOT public.cortex_identity_v2_valid_slug(public.cortex_identity_base(NEW.name)) THEN
        RAISE EXCEPTION 'Invalid Cortex agent identity % for project %', NEW.name, canonical_project.project_key
            USING ERRCODE = 'check_violation';
    END IF;

    canonical_actor := public.cortex_identity_v2_ensure_actor(
        canonical_project.project_key,
        NEW.name,
        'agents.name'
    );
    UPDATE public.cortex_actors
       SET status = 'active', updated_at = now()
     WHERE id = canonical_actor
       AND status <> 'active';

    NEW.project := canonical_project.project_key;
    NEW.project_id := canonical_project.id;
    NEW.actor_id := canonical_actor;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.cortex_agents_sync_identity() FROM PUBLIC;

DROP TRIGGER IF EXISTS cortex_agents_sync_identity_trigger ON public.agents;
CREATE TRIGGER cortex_agents_sync_identity_trigger
BEFORE INSERT OR UPDATE OF name, project ON public.agents
FOR EACH ROW EXECUTE FUNCTION public.cortex_agents_sync_identity();

-- Converge legacy text-only rows that are valid canonical identities. Invalid
-- historical aliases remain readable history but cannot receive credentials.
UPDATE public.agents AS agent
   SET project = agent.project
 WHERE public.cortex_identity_v2_valid_slug(public.cortex_identity_base(agent.name))
   AND position(':' in lower(btrim(agent.name))) = 0
   AND EXISTS (
       SELECT 1
         FROM public.cortex_projects AS project
        WHERE project.project_key = lower(btrim(agent.project))
   );
