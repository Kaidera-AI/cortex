-- Roster removal is an authorization event, not only a UI visibility change.
-- Disable every matching ServiceAuth grant and revoke its tokens atomically with
-- the agent row update so a history-only identity cannot keep authenticating.

CREATE OR REPLACE FUNCTION cortex_auth.disable_removed_agent_credentials()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO cortex_auth, public
AS $$
DECLARE
    principal record;
    auth_generation bigint;
    disabled_timestamp timestamptz := now();
BEGIN
    IF NEW.status IN ('disabled', 'deleted', 'archived', 'retired')
       OR COALESCE(NEW.capabilities->>'visibility', 'active') = 'history-only'
       OR COALESCE(NEW.capabilities->>'keep_visible', 'false') <> 'true' THEN
        SELECT generation
          INTO auth_generation
          FROM cortex_auth.state
         WHERE singleton;

        FOR principal IN
            SELECT service_principal.id
              FROM cortex_auth.principals AS service_principal
              JOIN cortex_auth.grants AS service_grant
                ON service_grant.principal_id = service_principal.id
             WHERE service_principal.agent_id = NEW.id
               AND service_grant.disabled_at IS NULL
             FOR UPDATE OF service_grant
        LOOP
            UPDATE cortex_auth.grants
               SET disabled_at = disabled_timestamp,
                   updated_at = disabled_timestamp
             WHERE principal_id = principal.id;
            UPDATE cortex_auth.tokens
               SET revoked_at = COALESCE(revoked_at, disabled_timestamp)
             WHERE principal_id = principal.id;
            INSERT INTO cortex_auth.audit (
                occurred_at, action, principal_id, token_id, generation
            ) VALUES (
                disabled_timestamp,
                'principal_disabled_roster_removal',
                principal.id,
                NULL,
                auth_generation
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION cortex_auth.disable_removed_agent_credentials() FROM PUBLIC;

DROP TRIGGER IF EXISTS cortex_agents_disable_service_auth_trigger ON public.agents;
CREATE TRIGGER cortex_agents_disable_service_auth_trigger
AFTER UPDATE OF capabilities, status ON public.agents
FOR EACH ROW EXECUTE FUNCTION cortex_auth.disable_removed_agent_credentials();
