-- Identity v2 clean cutover.
--
-- Removes denormalized project/customer hex from live schema. Legacy values are
-- archived as provenance only; project identity is project_id/project_key plus
-- agent@project display identity.

BEGIN;

CREATE TABLE IF NOT EXISTS cortex_legacy_identity_archive (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    archived_at timestamptz NOT NULL DEFAULT now(),
    archive_reason text NOT NULL,
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_pk text,
    project_key text,
    project_id uuid,
    legacy_project_hex text,
    legacy_customer_hex text,
    legacy_identity text,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_cortex_legacy_identity_archive_source
    ON cortex_legacy_identity_archive (source_schema, source_table, source_pk);
CREATE INDEX IF NOT EXISTS idx_cortex_legacy_identity_archive_project
    ON cortex_legacy_identity_archive (project_id, project_key);

-- Actor aliases: archive and remove old colon/hex aliases from the live resolver.
INSERT INTO cortex_legacy_identity_archive (
    archive_reason, source_schema, source_table, source_pk,
    project_id, legacy_identity, legacy_payload
)
SELECT
    'identity-v2-clean-cutover-actor-alias',
    'public',
    'cortex_actor_aliases',
    id::text,
    project_id,
    alias_text,
    to_jsonb(cortex_actor_aliases)
FROM cortex_actor_aliases
WHERE alias_kind = 'legacy-hex'
   OR alias_text ~ '^[a-z][a-z0-9_-]*:'
ON CONFLICT DO NOTHING;

DELETE FROM cortex_actor_aliases
WHERE alias_kind = 'legacy-hex'
   OR alias_text ~ '^[a-z][a-z0-9_-]*:';

ALTER TABLE cortex_actor_aliases
    DROP CONSTRAINT IF EXISTS ck_cortex_actor_aliases_kind;
ALTER TABLE cortex_actor_aliases
    ADD CONSTRAINT ck_cortex_actor_aliases_kind
    CHECK (alias_kind = ANY (ARRAY['display'::text, 'bare'::text, 'observed'::text]));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cortex_actor_aliases'::regclass
           AND conname = 'ck_cortex_actor_aliases_no_colon'
    ) THEN
        ALTER TABLE cortex_actor_aliases
            ADD CONSTRAINT ck_cortex_actor_aliases_no_colon
            CHECK (position(':' in alias_text) = 0);
    END IF;
END $$;

-- Recreate identity functions without project_hex dependencies or legacy alias writes.
CREATE OR REPLACE FUNCTION public.cortex_identity_v2_ensure_actor(
    p_project text,
    p_identity text,
    p_source text DEFAULT 'write'::text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
DECLARE
    v_project record;
    v_actor_id uuid;
    v_slug text;
    v_display text;
    v_observed text;
BEGIN
    IF p_identity IS NULL OR btrim(p_identity) = '' THEN
        RETURN NULL;
    END IF;

    v_observed := lower(btrim(p_identity));
    IF position(':' in v_observed) > 0 THEN
        RAISE EXCEPTION 'Colon-suffixed Cortex identity % is retired for project %', p_identity, p_project
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT id, project_key
      INTO v_project
      FROM cortex_projects
     WHERE project_key = lower(btrim(p_project));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cortex project % is not registered', p_project
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    v_slug := cortex_identity_base(v_observed);
    IF NOT cortex_identity_v2_valid_slug(v_slug) THEN
        RAISE EXCEPTION 'Invalid Cortex actor identity % for project %', p_identity, p_project
            USING ERRCODE = 'check_violation';
    END IF;
    v_display := cortex_identity_display(v_slug, v_project.project_key);

    INSERT INTO cortex_actors (
        project_id, slug, kind, status, display_name, metadata
    )
    VALUES (
        v_project.id,
        v_slug,
        'agent',
        'historical',
        v_display,
        jsonb_build_object('sources', jsonb_build_array(p_source))
    )
    ON CONFLICT (project_id, slug, kind) DO UPDATE SET
        display_name = EXCLUDED.display_name,
        metadata = cortex_actors.metadata || jsonb_build_object('last_source', p_source),
        updated_at = now()
    RETURNING id INTO v_actor_id;

    INSERT INTO cortex_actor_aliases (project_id, actor_id, alias_text, alias_kind, source)
    VALUES
        (v_project.id, v_actor_id, v_display, 'display', p_source),
        (v_project.id, v_actor_id, v_slug, 'bare', p_source),
        (v_project.id, v_actor_id, v_observed, 'observed', p_source)
    ON CONFLICT (project_id, lower(alias_text)) DO NOTHING;

    RETURN v_actor_id;
END;
$$;

DROP TRIGGER IF EXISTS trg_identity_v2_handoffs ON handoffs;

CREATE OR REPLACE FUNCTION public.cortex_identity_v2_normalize_handoff_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
DECLARE
    cp record;
    slug text;
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

    slug := cortex_identity_base(NEW.from_agent);
    IF NOT cortex_identity_v2_valid_slug(slug) THEN
        RAISE EXCEPTION 'Invalid Cortex handoff from_agent % for project %', NEW.from_agent, NEW.project
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.from_actor_id := cortex_identity_v2_ensure_actor(cp.project_key, NEW.from_agent, TG_TABLE_NAME || '.from_agent');
    NEW.from_agent := cortex_identity_display(slug, cp.project_key);

    IF NEW.to_agent IS NOT NULL AND btrim(NEW.to_agent) <> '' THEN
        slug := cortex_identity_base(NEW.to_agent);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex handoff to_agent % for project %', NEW.to_agent, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.to_actor_id := cortex_identity_v2_ensure_actor(cp.project_key, NEW.to_agent, TG_TABLE_NAME || '.to_agent');
        NEW.to_agent := cortex_identity_display(slug, cp.project_key);
    ELSE
        NEW.to_actor_id := NULL;
    END IF;

    IF NEW.claimed_by IS NOT NULL AND btrim(NEW.claimed_by) <> '' THEN
        slug := cortex_identity_base(NEW.claimed_by);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex handoff claimed_by % for project %', NEW.claimed_by, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.claimed_by_actor_id := cortex_identity_v2_ensure_actor(cp.project_key, NEW.claimed_by, TG_TABLE_NAME || '.claimed_by');
        NEW.claimed_by := cortex_identity_display(slug, cp.project_key);
    ELSE
        NEW.claimed_by_actor_id := NULL;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_identity_v2_handoffs
    BEFORE INSERT OR UPDATE OF project, from_agent, to_agent, claimed_by ON handoffs
    FOR EACH ROW EXECUTE FUNCTION cortex_identity_v2_normalize_handoff_row();

-- Public support tables: add canonical project_id, archive old scope, delete
-- unmatched legacy rows, then drop hex columns.
ALTER TABLE profile_bundles ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE memory_sync_events ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE harness_artifacts ADD COLUMN IF NOT EXISTS project_id uuid;

UPDATE profile_bundles pb
   SET project_id = cp.id
  FROM cortex_projects cp
 WHERE pb.project_id IS NULL
   AND cp.project_hex::text = pb.project_hex::text;

UPDATE memory_sync_events mse
   SET project_id = cp.id
  FROM cortex_projects cp
 WHERE mse.project_id IS NULL
   AND cp.project_hex::text = mse.project_hex::text;

UPDATE harness_artifacts ha
   SET project_id = cp.id
  FROM cortex_projects cp
 WHERE ha.project_id IS NULL
   AND cp.project_hex::text = ha.project_hex::text;

INSERT INTO cortex_legacy_identity_archive (
    archive_reason, source_schema, source_table, source_pk,
    project_id, legacy_project_hex, legacy_customer_hex, legacy_payload
)
SELECT 'identity-v2-clean-cutover-public-support', 'public', 'profile_bundles', id::text,
       project_id, project_hex::text, customer_hex::text, to_jsonb(profile_bundles)
  FROM profile_bundles
 WHERE project_hex IS NOT NULL OR customer_hex IS NOT NULL
UNION ALL
SELECT 'identity-v2-clean-cutover-public-support', 'public', 'memory_sync_events', id::text,
       project_id, project_hex::text, customer_hex::text, to_jsonb(memory_sync_events)
  FROM memory_sync_events
 WHERE project_hex IS NOT NULL OR customer_hex IS NOT NULL
UNION ALL
SELECT 'identity-v2-clean-cutover-public-support', 'public', 'harness_artifacts', id::text,
       project_id, project_hex::text, customer_hex::text, to_jsonb(harness_artifacts)
  FROM harness_artifacts
 WHERE project_hex IS NOT NULL OR customer_hex IS NOT NULL;

DELETE FROM profile_bundles WHERE project_id IS NULL;
DELETE FROM memory_sync_events WHERE project_id IS NULL;
DELETE FROM harness_artifacts WHERE project_id IS NULL;

ALTER TABLE profile_bundles ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE memory_sync_events ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE harness_artifacts ALTER COLUMN project_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'public.profile_bundles'::regclass
        AND conname = 'profile_bundles_project_id_fkey'
    ) THEN
        ALTER TABLE profile_bundles
            ADD CONSTRAINT profile_bundles_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES cortex_projects(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'public.memory_sync_events'::regclass
        AND conname = 'memory_sync_events_project_id_fkey'
    ) THEN
        ALTER TABLE memory_sync_events
            ADD CONSTRAINT memory_sync_events_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES cortex_projects(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'public.harness_artifacts'::regclass
        AND conname = 'harness_artifacts_project_id_fkey'
    ) THEN
        ALTER TABLE harness_artifacts
            ADD CONSTRAINT harness_artifacts_project_id_fkey
            FOREIGN KEY (project_id) REFERENCES cortex_projects(id) ON DELETE CASCADE;
    END IF;
END $$;

DROP POLICY IF EXISTS profile_bundles_project_isolation ON profile_bundles;
DROP POLICY IF EXISTS memory_sync_events_project_isolation ON memory_sync_events;
DROP POLICY IF EXISTS harness_artifacts_project_isolation ON harness_artifacts;

CREATE POLICY profile_bundles_project_isolation ON profile_bundles
    USING (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)))
    WITH CHECK (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)));
CREATE POLICY memory_sync_events_project_isolation ON memory_sync_events
    USING (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)))
    WITH CHECK (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)));
CREATE POLICY harness_artifacts_project_isolation ON harness_artifacts
    USING (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)))
    WITH CHECK (project_id = (SELECT id FROM cortex_projects WHERE project_key = current_setting('cortex.project', true)));

ALTER TABLE profile_bundles DROP CONSTRAINT IF EXISTS profile_bundles_customer_hex_project_hex_agent_name_version_key;
ALTER TABLE harness_artifacts DROP CONSTRAINT IF EXISTS harness_artifacts_customer_hex_project_hex_harness_path_key;
DROP INDEX IF EXISTS idx_profile_bundles_scope_agent;
DROP INDEX IF EXISTS idx_memory_sync_events_scope_created;
DROP INDEX IF EXISTS idx_harness_artifacts_scope_harness;

CREATE UNIQUE INDEX IF NOT EXISTS ux_profile_bundles_project_agent_version
    ON profile_bundles (project_id, agent_name, version);
CREATE INDEX IF NOT EXISTS idx_profile_bundles_project_agent
    ON profile_bundles (project_id, lower(agent_name));
CREATE INDEX IF NOT EXISTS idx_memory_sync_events_project_created
    ON memory_sync_events (project_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_harness_artifacts_project_harness_path
    ON harness_artifacts (project_id, harness, path);
CREATE INDEX IF NOT EXISTS idx_harness_artifacts_project_harness
    ON harness_artifacts (project_id, harness);

ALTER TABLE profile_bundles
    DROP COLUMN IF EXISTS customer_hex,
    DROP COLUMN IF EXISTS project_hex;
ALTER TABLE memory_sync_events
    DROP COLUMN IF EXISTS customer_hex,
    DROP COLUMN IF EXISTS project_hex;
ALTER TABLE harness_artifacts
    DROP COLUMN IF EXISTS customer_hex,
    DROP COLUMN IF EXISTS project_hex;

-- Public handoffs and projects.
INSERT INTO cortex_legacy_identity_archive (
    archive_reason, source_schema, source_table, source_pk,
    project_key, project_id, legacy_project_hex, legacy_payload
)
SELECT 'identity-v2-clean-cutover-public-handoffs', 'public', 'handoffs', h.id::text,
       h.project, h.project_id, h.project_hex, to_jsonb(h)
  FROM handoffs h
 WHERE h.project_hex IS NOT NULL;

ALTER TABLE handoffs DROP COLUMN IF EXISTS project_hex;

INSERT INTO cortex_legacy_identity_archive (
    archive_reason, source_schema, source_table, source_pk,
    project_key, project_id, legacy_project_hex, legacy_payload
)
SELECT 'identity-v2-clean-cutover-cortex-projects', 'public', 'cortex_projects', id::text,
       project_key, id, project_hex::text, to_jsonb(cortex_projects)
  FROM cortex_projects
 WHERE project_hex IS NOT NULL;

ALTER TABLE cortex_projects DROP CONSTRAINT IF EXISTS ck_cortex_projects_project_hex_format;
DROP INDEX IF EXISTS cortex_projects_project_hex_key;
ALTER TABLE cortex_projects DROP COLUMN IF EXISTS project_hex;
DROP FUNCTION IF EXISTS public.cortex_identity_v2_project_hex(text);

-- Cortex schema tenant tables: archive old hex mirrors, drop hex constraints,
-- replace indexes with UUID-based indexes, then remove the columns.
DO $$
DECLARE
    r record;
    id_expr text;
    idx_name text;
BEGIN
    FOR r IN
        SELECT DISTINCT table_name
          FROM information_schema.columns
         WHERE table_schema = 'cortex'
           AND column_name IN ('customer_hex', 'project_hex')
         ORDER BY table_name
    LOOP
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'cortex'
               AND table_name = r.table_name
               AND column_name = 'id'
        ) THEN 't.id::text' ELSE 't.ctid::text' END
        INTO id_expr;

        EXECUTE format(
            'INSERT INTO cortex_legacy_identity_archive (
                 archive_reason, source_schema, source_table, source_pk,
                 project_id, legacy_project_hex, legacy_customer_hex, legacy_payload
             )
             SELECT %L, %L, %L, %s, t.project_id, t.project_hex::text, t.customer_hex::text, to_jsonb(t)
               FROM cortex.%I t
              WHERE t.project_hex IS NOT NULL OR t.customer_hex IS NOT NULL',
            'identity-v2-clean-cutover-cortex-schema',
            'cortex',
            r.table_name,
            id_expr,
            r.table_name
        );
    END LOOP;

    FOR r IN
        SELECT conrelid::regclass::text AS table_ref, conname
          FROM pg_constraint
         WHERE connamespace = 'cortex'::regnamespace
           AND pg_get_constraintdef(oid) ILIKE '%hex%'
         ORDER BY conrelid::regclass::text, conname
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I', r.table_ref, r.conname);
    END LOOP;

    FOR r IN
        SELECT schemaname, indexname
          FROM pg_indexes
         WHERE schemaname = 'cortex'
           AND indexdef ILIKE '%hex%'
         ORDER BY schemaname, indexname
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I.%I', r.schemaname, r.indexname);
    END LOOP;

    FOR r IN
        SELECT DISTINCT table_name
          FROM information_schema.columns
         WHERE table_schema = 'cortex'
           AND column_name IN ('customer_hex', 'project_hex')
         ORDER BY table_name
    LOOP
        EXECUTE format('ALTER TABLE cortex.%I DROP COLUMN IF EXISTS customer_hex, DROP COLUMN IF EXISTS project_hex', r.table_name);
    END LOOP;

    FOR r IN
        SELECT table_name
          FROM information_schema.columns
         WHERE table_schema = 'cortex'
           AND column_name IN ('customer_id', 'project_id')
         GROUP BY table_name
        HAVING bool_or(column_name = 'customer_id') AND bool_or(column_name = 'project_id')
         ORDER BY table_name
    LOOP
        idx_name := left('ix_' || r.table_name || '_tenant_uuid', 60);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON cortex.%I (customer_id, project_id)', idx_name, r.table_name);
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_profiles_project_name_uuid
    ON cortex.agent_profiles (project_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifact_edges_project_identity
    ON cortex.artifact_edges (project_id, source_id, target_id, edge_type);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifacts_project_source_ref
    ON cortex.artifacts (project_id, source_ref) WHERE source_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cortex_tenant_quotas_tenant_uuid
    ON cortex.cortex_tenant_quotas (customer_id, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_customer_project_uuid
    ON cortex.projects (customer_id, project_id);

-- Refresh audit to flag live colon identity, missing project IDs, and missing actor IDs.
CREATE OR REPLACE VIEW cortex_identity_v2_audit AS
WITH project_rows(table_name, project, project_id) AS (
    SELECT 'decisions', project, project_id FROM decisions
    UNION ALL SELECT 'lessons', project, project_id FROM lessons
    UNION ALL SELECT 'team_events', project, project_id FROM team_events
    UNION ALL SELECT 'messages', project, project_id FROM messages
    UNION ALL SELECT 'work_products', project, project_id FROM work_products
    UNION ALL SELECT 'handoffs', project, project_id FROM handoffs
    UNION ALL SELECT 'tasks', project, project_id FROM tasks
    UNION ALL SELECT 'knowledge', project, project_id FROM knowledge
    UNION ALL SELECT 'artifacts', project, project_id FROM artifacts
    UNION ALL SELECT 'cortex_entities', project, project_id FROM cortex_entities
), identity_rows(table_name, project, column_name, identity_value, actor_id) AS (
    SELECT 'decisions', project, 'agent_name', agent_name, actor_id FROM decisions WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'lessons', project, 'agent_name', agent_name, actor_id FROM lessons WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'team_events', project, 'agent_name', agent_name, actor_id FROM team_events WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'messages', project, 'agent_name', agent_name, actor_id FROM messages WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'work_products', project, 'agent_name', agent_name, actor_id FROM work_products WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'from_agent', from_agent, from_actor_id FROM handoffs WHERE from_agent IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'to_agent', to_agent, to_actor_id FROM handoffs WHERE to_agent IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'claimed_by', claimed_by, claimed_by_actor_id FROM handoffs WHERE claimed_by IS NOT NULL
    UNION ALL SELECT 'tasks', project, 'assigned_agent', assigned_agent, assigned_actor_id FROM tasks WHERE assigned_agent IS NOT NULL
), alias_rows(table_name, project, column_name, identity_value, actor_id) AS (
    SELECT 'cortex_actor_aliases', cp.project_key, 'alias_text', ca.alias_text, ca.actor_id
      FROM cortex_actor_aliases ca
      JOIN cortex_projects cp ON cp.id = ca.project_id
)
SELECT table_name, project, NULL::text AS column_name, NULL::text AS identity_value,
       'missing_project_id'::text AS issue, count(*) AS row_count
  FROM project_rows
 WHERE project IS NOT NULL
   AND lower(btrim(project)) <> ALL (ARRAY['_global'::text, '_local_state'::text])
   AND project_id IS NULL
 GROUP BY table_name, project
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'legacy_colon_identity'::text AS issue, count(*) AS row_count
  FROM identity_rows
 WHERE identity_value ~ '^[a-z][a-z0-9_-]*:'
 GROUP BY table_name, project, column_name, identity_value
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'invalid_actor_slug'::text AS issue, count(*) AS row_count
  FROM identity_rows
 WHERE NOT cortex_identity_v2_valid_slug(cortex_identity_base(identity_value))
 GROUP BY table_name, project, column_name, identity_value
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'missing_actor_id'::text AS issue, count(*) AS row_count
  FROM identity_rows
 WHERE cortex_identity_v2_valid_slug(cortex_identity_base(identity_value))
   AND actor_id IS NULL
 GROUP BY table_name, project, column_name, identity_value
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'legacy_alias_identity'::text AS issue, count(*) AS row_count
  FROM alias_rows
 WHERE identity_value ~ '^[a-z][a-z0-9_-]*:'
 GROUP BY table_name, project, column_name, identity_value;

CREATE OR REPLACE VIEW cortex_identity_v2_audit_summary AS
SELECT issue, table_name, project, sum(row_count)::bigint AS row_count
  FROM cortex_identity_v2_audit
 GROUP BY issue, table_name, project
 ORDER BY issue, table_name, project;

-- End-state assertions: no live hex schema, no legacy alias resolver.
DO $$
DECLARE
    remaining text;
BEGIN
    SELECT string_agg(table_schema || '.' || table_name || '.' || column_name, ', ' ORDER BY table_schema, table_name, column_name)
      INTO remaining
      FROM information_schema.columns
     WHERE table_schema IN ('public', 'cortex')
       AND column_name IN ('project_hex', 'customer_hex');
    IF remaining IS NOT NULL THEN
        RAISE EXCEPTION 'Identity v2 clean cutover incomplete; remaining hex columns: %', remaining;
    END IF;

    IF EXISTS (
        SELECT 1 FROM cortex_actor_aliases
         WHERE alias_kind = 'legacy-hex'
            OR alias_text ~ '^[a-z][a-z0-9_-]*:'
    ) THEN
        RAISE EXCEPTION 'Identity v2 clean cutover incomplete; live legacy actor aliases remain';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.prokind = 'f'
           AND pg_get_functiondef(p.oid) ILIKE '%project_hex%'
    ) THEN
        RAISE EXCEPTION 'Identity v2 clean cutover incomplete; public function still references project_hex';
    END IF;
END $$;

COMMIT;
