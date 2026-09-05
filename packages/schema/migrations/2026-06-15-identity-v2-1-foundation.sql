BEGIN;

-- Cortex Identity v2
-- Canonical identity is project_id + actor_id. Human display is agent@project.
-- Legacy agent:hex aliases are retained for resolution, but no longer remain
-- the stored display form on historical memory rows.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION cortex_identity_base(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT NULLIF(regexp_replace(lower(btrim(COALESCE(value, ''))), '([:@]).*$', ''), '')
$$;

CREATE OR REPLACE FUNCTION cortex_identity_v2_valid_slug(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(value ~ '^(claude-subagent-[a-f0-9]{6,20}|[a-z][a-z0-9_-]{0,63})$', false)
$$;

CREATE OR REPLACE FUNCTION cortex_identity_display(agent_slug text, project_key text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT cortex_identity_base(agent_slug) || '@' || lower(btrim(project_key))
$$;

CREATE OR REPLACE FUNCTION cortex_identity_v2_project_hex(project_key text)
RETURNS char(4)
LANGUAGE plpgsql
AS $$
DECLARE
    salt integer := 0;
    candidate char(4);
BEGIN
    LOOP
        candidate := substring(md5('cortex-project:' || lower(project_key) || ':' || salt::text), 1, 4);
        IF NOT EXISTS (
            SELECT 1 FROM cortex_projects WHERE project_hex = candidate
        ) THEN
            RETURN candidate;
        END IF;
        salt := salt + 1;
        IF salt > 65535 THEN
            RAISE EXCEPTION 'Could not allocate deterministic project hex for %', project_key;
        END IF;
    END LOOP;
END;
$$;

-- Make every historical project reference enforceable. These rows are
-- archive/provenance registrations for already-present data, not active roster
-- entries.
WITH project_refs(project_key) AS (
    SELECT project FROM decisions
    UNION SELECT project FROM lessons
    UNION SELECT project FROM team_events
    UNION SELECT project FROM messages
    UNION SELECT project FROM work_products
    UNION SELECT project FROM handoffs
    UNION SELECT project FROM tasks
    UNION SELECT project FROM agent_sessions
    UNION SELECT project FROM knowledge
    UNION SELECT project FROM artifacts
    UNION SELECT project FROM artifact_edges
    UNION SELECT project FROM cortex_entities
    UNION SELECT project FROM cortex_relationships
    UNION SELECT project FROM agents
    UNION SELECT project FROM agent_profiles
    UNION SELECT project FROM agent_diaries
    UNION SELECT project FROM archive_messages
    UNION SELECT project FROM archive_events
    UNION SELECT project FROM archive_decisions
    UNION SELECT project FROM archive_lessons
    UNION SELECT project FROM archive_handoffs
),
missing AS (
    SELECT DISTINCT lower(btrim(project_key)) AS project_key
      FROM project_refs
     WHERE project_key IS NOT NULL
       AND lower(btrim(project_key)) NOT IN ('_global', '_local_state')
       AND lower(btrim(project_key)) ~ '^[a-z0-9][a-z0-9-]{1,63}$'
       AND NOT EXISTS (
            SELECT 1 FROM cortex_projects cp
             WHERE cp.project_key = lower(btrim(project_refs.project_key))
       )
)
INSERT INTO cortex_projects (
    project_key, project_hex, display_name, repo_root, repo_type, status, metadata
)
SELECT
    project_key,
    cortex_identity_v2_project_hex(project_key),
    project_key,
    '',
    'archive',
    'archived',
    jsonb_build_object(
        'identity_v2', jsonb_build_object(
            'auto_registered', true,
            'reason', 'historical rows referenced this project before Identity v2',
            'registered_at', now()
        )
    )
FROM missing
ON CONFLICT (project_key) DO NOTHING;

UPDATE cortex_projects
   SET project_hex = lower(project_hex::text)::char(4)
 WHERE project_hex IS NOT NULL
   AND project_hex::text <> lower(project_hex::text);

UPDATE cortex_projects cp
   SET project_hex = cortex_identity_v2_project_hex(cp.project_key)
 WHERE cp.project_hex IS NULL
    OR cp.project_hex::text !~ '^[a-f0-9]{4}$';

ALTER TABLE cortex_projects ALTER COLUMN project_hex SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_cortex_projects_project_hex_format'
    ) THEN
        ALTER TABLE cortex_projects
            ADD CONSTRAINT ck_cortex_projects_project_hex_format
            CHECK (project_hex::text ~ '^[a-f0-9]{4}$');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS cortex_actors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES cortex_projects(id) ON DELETE CASCADE,
    slug text NOT NULL,
    kind text NOT NULL DEFAULT 'agent',
    status text NOT NULL DEFAULT 'historical',
    display_name text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_cortex_actors_slug CHECK (cortex_identity_v2_valid_slug(slug)),
    CONSTRAINT ck_cortex_actors_kind CHECK (kind IN ('agent', 'human', 'system')),
    CONSTRAINT ck_cortex_actors_status CHECK (status IN ('active', 'historical', 'system', 'retired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cortex_actors_project_slug_kind
    ON cortex_actors(project_id, slug, kind);
CREATE INDEX IF NOT EXISTS idx_cortex_actors_project_status
    ON cortex_actors(project_id, status);

CREATE TABLE IF NOT EXISTS cortex_actor_aliases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES cortex_projects(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL REFERENCES cortex_actors(id) ON DELETE CASCADE,
    alias_text text NOT NULL,
    alias_kind text NOT NULL DEFAULT 'observed',
    source text NOT NULL DEFAULT 'identity-v2',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_cortex_actor_aliases_alias_nonempty CHECK (btrim(alias_text) <> ''),
    CONSTRAINT ck_cortex_actor_aliases_kind CHECK (
        alias_kind IN ('display', 'bare', 'legacy-hex', 'observed')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cortex_actor_aliases_project_alias
    ON cortex_actor_aliases(project_id, lower(alias_text));
CREATE INDEX IF NOT EXISTS idx_cortex_actor_aliases_actor
    ON cortex_actor_aliases(actor_id);

-- Add normalized columns. Legacy text columns remain for compatibility but are
-- rewritten to the new display form.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE agent_diaries ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE agent_diaries ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE artifact_edges ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE cortex_entities ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE cortex_relationships ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_actor_id uuid;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE team_events ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE team_events ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE work_products ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE work_products ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS from_actor_id uuid;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS to_actor_id uuid;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS claimed_by_actor_id uuid;
ALTER TABLE archive_messages ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE archive_messages ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE archive_events ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE archive_events ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE archive_lessons ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE archive_lessons ADD COLUMN IF NOT EXISTS actor_id uuid;
ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS from_actor_id uuid;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'agents', 'agent_profiles', 'agent_diaries', 'agent_sessions',
        'knowledge', 'artifacts', 'artifact_edges', 'cortex_entities',
        'cortex_relationships', 'tasks', 'decisions', 'lessons',
        'team_events', 'messages', 'work_products', 'handoffs',
        'archive_messages', 'archive_events', 'archive_decisions',
        'archive_lessons', 'archive_handoffs'
    ] LOOP
        EXECUTE format(
            'UPDATE %I t
                SET project_id = cp.id
               FROM cortex_projects cp
              WHERE lower(btrim(t.project)) = cp.project_key
                AND t.project_id IS DISTINCT FROM cp.id',
            table_name
        );
    END LOOP;
END $$;

-- Seed actors from every historical identity-bearing surface before rewriting
-- the text columns, so old aliases are preserved.
WITH raw(project_key, identity_value, source) AS (
    SELECT project, name, 'agents.name' FROM agents WHERE name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'agent_profiles.agent_name' FROM agent_profiles WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'agent_diaries.agent_name' FROM agent_diaries WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'decisions.agent_name' FROM decisions WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'lessons.agent_name' FROM lessons WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'team_events.agent_name' FROM team_events WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'messages.agent_name' FROM messages WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'work_products.agent_name' FROM work_products WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, from_agent, 'handoffs.from_agent' FROM handoffs WHERE from_agent IS NOT NULL
    UNION ALL SELECT project, to_agent, 'handoffs.to_agent' FROM handoffs WHERE to_agent IS NOT NULL
    UNION ALL SELECT project, claimed_by, 'handoffs.claimed_by' FROM handoffs WHERE claimed_by IS NOT NULL
    UNION ALL SELECT project, assigned_agent, 'tasks.assigned_agent' FROM tasks WHERE assigned_agent IS NOT NULL
    UNION ALL SELECT project, agent_name, 'archive_messages.agent_name' FROM archive_messages WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'archive_events.agent_name' FROM archive_events WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'archive_decisions.agent_name' FROM archive_decisions WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, agent_name, 'archive_lessons.agent_name' FROM archive_lessons WHERE agent_name IS NOT NULL
    UNION ALL SELECT project, from_agent, 'archive_handoffs.from_agent' FROM archive_handoffs WHERE from_agent IS NOT NULL
),
norm AS (
    SELECT
        cp.id AS project_id,
        cp.project_key,
        cortex_identity_base(identity_value) AS slug,
        bool_or(source IN ('agents.name', 'agent_profiles.agent_name')) AS active_source,
        jsonb_agg(DISTINCT source) AS sources
      FROM raw
      JOIN cortex_projects cp ON cp.project_key = lower(btrim(raw.project_key))
     WHERE raw.project_key IS NOT NULL
       AND cortex_identity_v2_valid_slug(cortex_identity_base(identity_value))
     GROUP BY cp.id, cp.project_key, cortex_identity_base(identity_value)
)
INSERT INTO cortex_actors (
    project_id, slug, kind, status, display_name, metadata
)
SELECT
    project_id,
    slug,
    'agent',
    CASE WHEN active_source THEN 'active' ELSE 'historical' END,
    cortex_identity_display(slug, project_key),
    jsonb_build_object('sources', sources)
FROM norm
ON CONFLICT (project_id, slug, kind) DO UPDATE SET
    status = CASE
        WHEN cortex_actors.status = 'active' OR EXCLUDED.status = 'active' THEN 'active'
        ELSE cortex_actors.status
    END,
    display_name = EXCLUDED.display_name,
    metadata = cortex_actors.metadata || EXCLUDED.metadata,
    updated_at = now();

WITH actor_rows AS (
    SELECT a.id AS actor_id, a.project_id, a.slug, cp.project_key, cp.project_hex::text AS project_hex
      FROM cortex_actors a
      JOIN cortex_projects cp ON cp.id = a.project_id
     WHERE a.kind = 'agent'
),
aliases AS (
    SELECT project_id, actor_id, cortex_identity_display(slug, project_key) AS alias_text, 'display' AS alias_kind, 'identity-v2' AS source
      FROM actor_rows
    UNION ALL
    SELECT project_id, actor_id, slug, 'bare', 'identity-v2'
      FROM actor_rows
    UNION ALL
    SELECT project_id, actor_id, slug || ':' || project_hex, 'legacy-hex', 'identity-v2'
      FROM actor_rows
),
observed AS (
    WITH raw(project_key, identity_value, source) AS (
        SELECT project, name, 'agents.name' FROM agents WHERE name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'agent_profiles.agent_name' FROM agent_profiles WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'agent_diaries.agent_name' FROM agent_diaries WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'decisions.agent_name' FROM decisions WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'lessons.agent_name' FROM lessons WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'team_events.agent_name' FROM team_events WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'messages.agent_name' FROM messages WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, agent_name, 'work_products.agent_name' FROM work_products WHERE agent_name IS NOT NULL
        UNION ALL SELECT project, from_agent, 'handoffs.from_agent' FROM handoffs WHERE from_agent IS NOT NULL
        UNION ALL SELECT project, to_agent, 'handoffs.to_agent' FROM handoffs WHERE to_agent IS NOT NULL
        UNION ALL SELECT project, claimed_by, 'handoffs.claimed_by' FROM handoffs WHERE claimed_by IS NOT NULL
        UNION ALL SELECT project, assigned_agent, 'tasks.assigned_agent' FROM tasks WHERE assigned_agent IS NOT NULL
    )
    SELECT
        a.project_id,
        a.id AS actor_id,
        lower(btrim(raw.identity_value)) AS alias_text,
        'observed' AS alias_kind,
        raw.source
      FROM raw
      JOIN cortex_projects cp ON cp.project_key = lower(btrim(raw.project_key))
      JOIN cortex_actors a
        ON a.project_id = cp.id
       AND a.slug = cortex_identity_base(raw.identity_value)
       AND a.kind = 'agent'
     WHERE raw.identity_value IS NOT NULL
       AND btrim(raw.identity_value) <> ''
       AND cortex_identity_v2_valid_slug(cortex_identity_base(raw.identity_value))
)
INSERT INTO cortex_actor_aliases (
    project_id, actor_id, alias_text, alias_kind, source
)
SELECT DISTINCT project_id, actor_id, alias_text, alias_kind, source FROM aliases
UNION
SELECT DISTINCT project_id, actor_id, alias_text, alias_kind, source FROM observed
ON CONFLICT (project_id, lower(alias_text)) DO NOTHING;

CREATE OR REPLACE FUNCTION cortex_identity_v2_transform_column(p_table regclass, p_column name)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    affected bigint := 0;
BEGIN
    EXECUTE format(
        'UPDATE %s t
            SET %I = cortex_identity_display(cortex_identity_base(t.%I), t.project)
          WHERE t.project IS NOT NULL
            AND lower(btrim(t.project)) NOT IN (''_global'', ''_local_state'')
            AND t.%I IS NOT NULL
            AND cortex_identity_v2_valid_slug(cortex_identity_base(t.%I))
            AND t.%I IS DISTINCT FROM cortex_identity_display(cortex_identity_base(t.%I), t.project)',
        p_table,
        p_column,
        p_column,
        p_column,
        p_column,
        p_column,
        p_column
    );
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$;

SELECT cortex_identity_v2_transform_column('decisions', 'agent_name');
SELECT cortex_identity_v2_transform_column('lessons', 'agent_name');
SELECT cortex_identity_v2_transform_column('team_events', 'agent_name');
SELECT cortex_identity_v2_transform_column('messages', 'agent_name');
SELECT cortex_identity_v2_transform_column('work_products', 'agent_name');
SELECT cortex_identity_v2_transform_column('handoffs', 'from_agent');
SELECT cortex_identity_v2_transform_column('handoffs', 'to_agent');
SELECT cortex_identity_v2_transform_column('handoffs', 'claimed_by');
SELECT cortex_identity_v2_transform_column('tasks', 'assigned_agent');
SELECT cortex_identity_v2_transform_column('archive_messages', 'agent_name');
SELECT cortex_identity_v2_transform_column('archive_events', 'agent_name');
SELECT cortex_identity_v2_transform_column('archive_decisions', 'agent_name');
SELECT cortex_identity_v2_transform_column('archive_lessons', 'agent_name');
SELECT cortex_identity_v2_transform_column('archive_handoffs', 'from_agent');

CREATE OR REPLACE FUNCTION cortex_identity_v2_backfill_actor_column(
    p_table regclass,
    p_identity_column name,
    p_actor_column name
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    affected bigint := 0;
BEGIN
    EXECUTE format(
        'UPDATE %s t
            SET %I = a.id
           FROM cortex_actors a
          WHERE t.project_id = a.project_id
            AND a.kind = ''agent''
            AND t.%I IS NOT NULL
            AND cortex_identity_v2_valid_slug(cortex_identity_base(t.%I))
            AND a.slug = cortex_identity_base(t.%I)
            AND t.%I IS DISTINCT FROM a.id',
        p_table,
        p_actor_column,
        p_identity_column,
        p_identity_column,
        p_identity_column,
        p_actor_column
    );
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$;

SELECT cortex_identity_v2_backfill_actor_column('agents', 'name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('agent_profiles', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('agent_diaries', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('decisions', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('lessons', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('team_events', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('messages', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('work_products', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('handoffs', 'from_agent', 'from_actor_id');
SELECT cortex_identity_v2_backfill_actor_column('handoffs', 'to_agent', 'to_actor_id');
SELECT cortex_identity_v2_backfill_actor_column('handoffs', 'claimed_by', 'claimed_by_actor_id');
SELECT cortex_identity_v2_backfill_actor_column('tasks', 'assigned_agent', 'assigned_actor_id');
SELECT cortex_identity_v2_backfill_actor_column('archive_messages', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('archive_events', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('archive_decisions', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('archive_lessons', 'agent_name', 'actor_id');
SELECT cortex_identity_v2_backfill_actor_column('archive_handoffs', 'from_agent', 'from_actor_id');

UPDATE handoffs h
   SET project_hex = cp.project_hex::text
  FROM cortex_projects cp
 WHERE h.project_id = cp.id
   AND h.project_hex IS DISTINCT FROM cp.project_hex::text;

CREATE OR REPLACE FUNCTION cortex_identity_v2_ensure_actor(
    p_project text,
    p_identity text,
    p_source text DEFAULT 'write'
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    cp record;
    actor uuid;
    slug text;
    display text;
BEGIN
    IF p_identity IS NULL OR btrim(p_identity) = '' THEN
        RETURN NULL;
    END IF;

    SELECT id, project_key, project_hex::text AS project_hex
      INTO cp
      FROM cortex_projects
     WHERE project_key = lower(btrim(p_project));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cortex project % is not registered', p_project
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    slug := cortex_identity_base(p_identity);
    IF NOT cortex_identity_v2_valid_slug(slug) THEN
        RAISE EXCEPTION 'Invalid Cortex actor identity % for project %', p_identity, p_project
            USING ERRCODE = 'check_violation';
    END IF;
    display := cortex_identity_display(slug, cp.project_key);

    INSERT INTO cortex_actors (
        project_id, slug, kind, status, display_name, metadata
    )
    VALUES (
        cp.id,
        slug,
        'agent',
        'historical',
        display,
        jsonb_build_object('sources', jsonb_build_array(p_source))
    )
    ON CONFLICT (project_id, slug, kind) DO UPDATE SET
        display_name = EXCLUDED.display_name,
        metadata = cortex_actors.metadata || jsonb_build_object('last_source', p_source),
        updated_at = now()
    RETURNING id INTO actor;

    INSERT INTO cortex_actor_aliases (project_id, actor_id, alias_text, alias_kind, source)
    VALUES
        (cp.id, actor, display, 'display', p_source),
        (cp.id, actor, slug, 'bare', p_source),
        (cp.id, actor, slug || ':' || cp.project_hex, 'legacy-hex', p_source),
        (cp.id, actor, lower(btrim(p_identity)), 'observed', p_source)
    ON CONFLICT (project_id, lower(alias_text)) DO NOTHING;

    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION cortex_identity_v2_normalize_agent_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
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
    IF NEW.agent_name IS NOT NULL THEN
        slug := cortex_identity_base(NEW.agent_name);
        IF NOT cortex_identity_v2_valid_slug(slug) THEN
            RAISE EXCEPTION 'Invalid Cortex actor identity % for project %', NEW.agent_name, NEW.project
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.actor_id := cortex_identity_v2_ensure_actor(cp.project_key, NEW.agent_name, TG_TABLE_NAME || '.agent_name');
        NEW.agent_name := cortex_identity_display(slug, cp.project_key);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION cortex_identity_v2_normalize_handoff_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    cp record;
    slug text;
BEGIN
    IF NEW.project IS NULL OR lower(btrim(NEW.project)) IN ('_global', '_local_state') THEN
        RETURN NEW;
    END IF;

    SELECT id, project_key, project_hex::text AS project_hex
      INTO cp
      FROM cortex_projects
     WHERE project_key = lower(btrim(NEW.project));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cortex project % is not registered', NEW.project
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    NEW.project_id := cp.id;
    NEW.project_hex := cp.project_hex;

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

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'decisions', 'lessons', 'team_events', 'messages', 'work_products',
        'archive_messages', 'archive_events', 'archive_decisions', 'archive_lessons'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_identity_v2_%I ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_identity_v2_%I
                 BEFORE INSERT OR UPDATE OF project, agent_name ON %I
                 FOR EACH ROW
                 EXECUTE FUNCTION cortex_identity_v2_normalize_agent_row()',
            table_name,
            table_name
        );
    END LOOP;
END $$;

DROP TRIGGER IF EXISTS trg_identity_v2_handoffs ON handoffs;
CREATE TRIGGER trg_identity_v2_handoffs
    BEFORE INSERT OR UPDATE OF project, project_hex, from_agent, to_agent, claimed_by ON handoffs
    FOR EACH ROW
    EXECUTE FUNCTION cortex_identity_v2_normalize_handoff_row();

DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('agents', 'project_id', 'cortex_projects', 'id'),
            ('agent_profiles', 'project_id', 'cortex_projects', 'id'),
            ('agent_diaries', 'project_id', 'cortex_projects', 'id'),
            ('agent_sessions', 'project_id', 'cortex_projects', 'id'),
            ('knowledge', 'project_id', 'cortex_projects', 'id'),
            ('artifacts', 'project_id', 'cortex_projects', 'id'),
            ('artifact_edges', 'project_id', 'cortex_projects', 'id'),
            ('cortex_entities', 'project_id', 'cortex_projects', 'id'),
            ('cortex_relationships', 'project_id', 'cortex_projects', 'id'),
            ('tasks', 'project_id', 'cortex_projects', 'id'),
            ('decisions', 'project_id', 'cortex_projects', 'id'),
            ('lessons', 'project_id', 'cortex_projects', 'id'),
            ('team_events', 'project_id', 'cortex_projects', 'id'),
            ('messages', 'project_id', 'cortex_projects', 'id'),
            ('work_products', 'project_id', 'cortex_projects', 'id'),
            ('handoffs', 'project_id', 'cortex_projects', 'id'),
            ('archive_messages', 'project_id', 'cortex_projects', 'id'),
            ('archive_events', 'project_id', 'cortex_projects', 'id'),
            ('archive_decisions', 'project_id', 'cortex_projects', 'id'),
            ('archive_lessons', 'project_id', 'cortex_projects', 'id'),
            ('archive_handoffs', 'project_id', 'cortex_projects', 'id'),
            ('agents', 'actor_id', 'cortex_actors', 'id'),
            ('agent_profiles', 'actor_id', 'cortex_actors', 'id'),
            ('agent_diaries', 'actor_id', 'cortex_actors', 'id'),
            ('tasks', 'assigned_actor_id', 'cortex_actors', 'id'),
            ('decisions', 'actor_id', 'cortex_actors', 'id'),
            ('lessons', 'actor_id', 'cortex_actors', 'id'),
            ('team_events', 'actor_id', 'cortex_actors', 'id'),
            ('messages', 'actor_id', 'cortex_actors', 'id'),
            ('work_products', 'actor_id', 'cortex_actors', 'id'),
            ('handoffs', 'from_actor_id', 'cortex_actors', 'id'),
            ('handoffs', 'to_actor_id', 'cortex_actors', 'id'),
            ('handoffs', 'claimed_by_actor_id', 'cortex_actors', 'id'),
            ('archive_messages', 'actor_id', 'cortex_actors', 'id'),
            ('archive_events', 'actor_id', 'cortex_actors', 'id'),
            ('archive_decisions', 'actor_id', 'cortex_actors', 'id'),
            ('archive_lessons', 'actor_id', 'cortex_actors', 'id'),
            ('archive_handoffs', 'from_actor_id', 'cortex_actors', 'id')
        ) AS v(table_name, column_name, ref_table, ref_column)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = format('fk_identity_v2_%s_%s', item.table_name, item.column_name)
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %I(%I) NOT VALID',
                item.table_name,
                format('fk_identity_v2_%s_%s', item.table_name, item.column_name),
                item.column_name,
                item.ref_table,
                item.ref_column
            );
        END IF;
        EXECUTE format(
            'ALTER TABLE %I VALIDATE CONSTRAINT %I',
            item.table_name,
            format('fk_identity_v2_%s_%s', item.table_name, item.column_name)
        );
    END LOOP;
END $$;

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
),
identity_rows(table_name, project, column_name, identity_value, actor_id) AS (
    SELECT 'decisions', project, 'agent_name', agent_name, actor_id FROM decisions WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'lessons', project, 'agent_name', agent_name, actor_id FROM lessons WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'team_events', project, 'agent_name', agent_name, actor_id FROM team_events WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'messages', project, 'agent_name', agent_name, actor_id FROM messages WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'work_products', project, 'agent_name', agent_name, actor_id FROM work_products WHERE agent_name IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'from_agent', from_agent, from_actor_id FROM handoffs WHERE from_agent IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'to_agent', to_agent, to_actor_id FROM handoffs WHERE to_agent IS NOT NULL
    UNION ALL SELECT 'handoffs', project, 'claimed_by', claimed_by, claimed_by_actor_id FROM handoffs WHERE claimed_by IS NOT NULL
    UNION ALL SELECT 'tasks', project, 'assigned_agent', assigned_agent, assigned_actor_id FROM tasks WHERE assigned_agent IS NOT NULL
)
SELECT table_name, project, NULL::text AS column_name, NULL::text AS identity_value,
       'missing_project_id' AS issue, count(*)::bigint AS row_count
  FROM project_rows
 WHERE project IS NOT NULL
   AND lower(btrim(project)) NOT IN ('_global', '_local_state')
   AND project_id IS NULL
 GROUP BY table_name, project
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'legacy_colon_identity' AS issue, count(*)::bigint AS row_count
  FROM identity_rows
 WHERE identity_value ~ '^[a-z][a-z0-9_-]*:'
 GROUP BY table_name, project, column_name, identity_value
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'invalid_actor_slug' AS issue, count(*)::bigint AS row_count
  FROM identity_rows
 WHERE NOT cortex_identity_v2_valid_slug(cortex_identity_base(identity_value))
 GROUP BY table_name, project, column_name, identity_value
UNION ALL
SELECT table_name, project, column_name, identity_value,
       'missing_actor_id' AS issue, count(*)::bigint AS row_count
  FROM identity_rows
 WHERE cortex_identity_v2_valid_slug(cortex_identity_base(identity_value))
   AND actor_id IS NULL
 GROUP BY table_name, project, column_name, identity_value;

CREATE OR REPLACE VIEW cortex_identity_v2_audit_summary AS
SELECT issue, table_name, project, sum(row_count)::bigint AS row_count
  FROM cortex_identity_v2_audit
 GROUP BY issue, table_name, project
 ORDER BY issue, table_name, project;

GRANT SELECT ON cortex_actors, cortex_actor_aliases TO cortex_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON cortex_actors, cortex_actor_aliases TO cortex_app;
GRANT SELECT ON cortex_identity_v2_audit, cortex_identity_v2_audit_summary TO cortex_app, cortex_reader;

COMMIT;
