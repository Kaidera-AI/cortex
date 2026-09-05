--
-- PostgreSQL database dump
--

\restrict b21kmeFBmTyjubhrzziLCMw7SQ1xFcNHoLxHkZbDodcNON273CfHhhpz7w65hAd

-- Dumped from database version 16.13 (Debian 16.13-1.pgdg12+1)
-- Dumped by pg_dump version 16.13 (Debian 16.13-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: cortex; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA cortex;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: agent_diaries_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.agent_diaries_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.summary, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: artifacts_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.artifacts_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(coalesce(NEW.title, '') || ' ' || NEW.content, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: captured_patterns_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.captured_patterns_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.name || ' ' || NEW.description, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: decisions_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.decisions_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.summary, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: execution_analyses_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.execution_analyses_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.summary, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: handoffs_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.handoffs_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.summary, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: knowledge_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.knowledge_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.title || ' ' || NEW.body, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: lessons_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.lessons_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.summary, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: messages_update_search_vector(); Type: FUNCTION; Schema: cortex; Owner: -
--

CREATE FUNCTION cortex.messages_update_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.content, ''));
          RETURN NEW;
        END
        $$;


--
-- Name: cortex_identity_base(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_base(value text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $_$
    SELECT NULLIF(regexp_replace(lower(btrim(COALESCE(value, ''))), '([:@]).*$', ''), '')
$_$;


--
-- Name: cortex_identity_display(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_display(agent_slug text, project_key text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT cortex_identity_base(agent_slug) || '@' || lower(btrim(project_key))
$$;


--
-- Name: cortex_identity_v2_backfill_actor_column(regclass, name, name); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_backfill_actor_column(p_table regclass, p_identity_column name, p_actor_column name) RETURNS bigint
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


--
-- Name: cortex_identity_v2_ensure_actor(text, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_ensure_actor(p_project text, p_identity text, p_source text DEFAULT 'write'::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
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


--
-- Name: cortex_identity_v2_normalize_agent_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_normalize_agent_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
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


--
-- Name: cortex_identity_v2_normalize_handoff_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_normalize_handoff_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
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


--
-- Name: cortex_identity_v2_transform_column(regclass, name); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_transform_column(p_table regclass, p_column name) RETURNS bigint
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


--
-- Name: cortex_identity_v2_valid_slug(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_identity_v2_valid_slug(value text) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $_$
    SELECT COALESCE(value ~ '^(claude-subagent-[a-f0-9]{6,20}|[a-z][a-z0-9_-]{0,63})$', false)
$_$;


--
-- Name: cortex_tsvector_captured_patterns(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_tsvector_captured_patterns() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.title, '') || ' ' || coalesce(NEW.description, '') || ' ' || coalesce(NEW.solution, '')
    );
    RETURN NEW;
END;
$$;


--
-- Name: cortex_tsvector_decisions(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_tsvector_decisions() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.summary, '') || ' ' || coalesce(NEW.rationale, '')
    );
    RETURN NEW;
END;
$$;


--
-- Name: cortex_tsvector_knowledge(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_tsvector_knowledge() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.content, '')
    );
    RETURN NEW;
END;
$$;


--
-- Name: cortex_tsvector_lessons(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_tsvector_lessons() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.summary, '') || ' ' || coalesce(NEW.detail, '')
    );
    RETURN NEW;
END;
$$;


--
-- Name: cortex_tsvector_messages(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cortex_tsvector_messages() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.content, '')
    );
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_diaries; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.agent_diaries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_name character varying(120) NOT NULL,
    summary text NOT NULL,
    outcome character varying(32) NOT NULL,
    importance integer DEFAULT 5 NOT NULL,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: agent_knowledge_sources; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.agent_knowledge_sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    source_type character varying(32) NOT NULL,
    source_ref text NOT NULL,
    ingestion_status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    last_ingested_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: agent_profiles; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.agent_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(120) NOT NULL,
    role character varying(120) NOT NULL,
    lane character varying(120),
    reports_to character varying(120),
    capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: alembic_version; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: analysis_cost_log; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.analysis_cost_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    operation character varying(64) NOT NULL,
    model character varying(120),
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cost_usd double precision DEFAULT 0.0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: artifact_edges; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.artifact_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id uuid NOT NULL,
    target_id uuid NOT NULL,
    target_type character varying(32) DEFAULT 'artifact'::character varying NOT NULL,
    edge_type character varying(32) NOT NULL,
    confidence double precision DEFAULT 1.0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    edge_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: artifacts; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.artifacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    modality character varying(32) NOT NULL,
    source_file text,
    source_url text,
    extraction_method character varying(64),
    title character varying(512),
    content text NOT NULL,
    embedding public.vector(2048),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    search_vector tsvector,
    confident boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_ref text,
    customer_id uuid,
    project_id uuid,
    caption text,
    neighborhood_text text,
    source_doc_metadata jsonb
);


--
-- Name: captured_patterns; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.captured_patterns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) NOT NULL,
    description text NOT NULL,
    pattern_body text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    confident boolean DEFAULT true NOT NULL,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: cortex_cost_log; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.cortex_cost_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid,
    project_id uuid,
    kind character varying(32) NOT NULL,
    tokens_in integer DEFAULT 0 NOT NULL,
    tokens_out integer DEFAULT 0 NOT NULL,
    model character varying(120),
    cost_usd double precision DEFAULT 0.0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    agent_name character varying(120),
    request_id character varying(120),
    route text,
    tokens_input integer DEFAULT 0 NOT NULL,
    tokens_output integer DEFAULT 0 NOT NULL,
    latency_ms integer,
    vlm_calls integer,
    embedding_calls integer,
    CONSTRAINT ck_cortex_cost_log_cost_nonnegative CHECK ((cost_usd >= (0)::double precision)),
    CONSTRAINT ck_cortex_cost_log_embedding_calls_nonnegative CHECK (((embedding_calls IS NULL) OR (embedding_calls >= 0))),
    CONSTRAINT ck_cortex_cost_log_latency_nonnegative CHECK (((latency_ms IS NULL) OR (latency_ms >= 0))),
    CONSTRAINT ck_cortex_cost_log_tokens_io_nonnegative CHECK (((tokens_input >= 0) AND (tokens_output >= 0))),
    CONSTRAINT ck_cortex_cost_log_tokens_nonnegative CHECK (((tokens_in >= 0) AND (tokens_out >= 0))),
    CONSTRAINT ck_cortex_cost_log_vlm_calls_nonnegative CHECK (((vlm_calls IS NULL) OR (vlm_calls >= 0)))
);


--
-- Name: cortex_entities; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.cortex_entities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) NOT NULL,
    entity_type character varying(64) NOT NULL,
    description text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid,
    project text NOT NULL
);


--
-- Name: cortex_relationships; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.cortex_relationships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_entity_id uuid NOT NULL,
    target_entity_id uuid NOT NULL,
    relation_type character varying(64) NOT NULL,
    weight double precision DEFAULT 1.0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid,
    project text NOT NULL,
    relationship_type character varying(64)
);


--
-- Name: cortex_tenant_quotas; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.cortex_tenant_quotas (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid,
    project_id uuid,
    plan_tier character varying(32) DEFAULT 'starter'::character varying NOT NULL,
    vlm_monthly_limit integer,
    vlm_monthly_used integer DEFAULT 0 NOT NULL,
    vlm_period_start timestamp with time zone DEFAULT date_trunc('month'::text, now()) NOT NULL,
    vlm_topup_url text,
    rate_limits jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cortex_tenant_quotas_plan_tier CHECK (((plan_tier)::text = ANY ((ARRAY['starter'::character varying, 'pro'::character varying, 'enterprise'::character varying])::text[]))),
    CONSTRAINT ck_cortex_tenant_quotas_vlm_limit_nonnegative CHECK (((vlm_monthly_limit IS NULL) OR (vlm_monthly_limit >= 0))),
    CONSTRAINT ck_cortex_tenant_quotas_vlm_used_nonnegative CHECK ((vlm_monthly_used >= 0))
);


--
-- Name: decisions; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_name character varying(120) NOT NULL,
    summary text NOT NULL,
    rationale text,
    priority_tier integer DEFAULT 2 NOT NULL,
    confident boolean DEFAULT true NOT NULL,
    quality_score double precision DEFAULT 0.5 NOT NULL,
    times_selected integer DEFAULT 0 NOT NULL,
    parent_decision_id uuid,
    supersession_summary text,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: execution_analyses; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.execution_analyses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id character varying(120),
    agent_name character varying(120) NOT NULL,
    model_used character varying(120),
    summary text NOT NULL,
    findings jsonb DEFAULT '{}'::jsonb NOT NULL,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: handoffs; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.handoffs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    from_agent character varying(120) NOT NULL,
    from_role character varying(120),
    to_role character varying(120) NOT NULL,
    to_agent character varying(120),
    priority character varying(16) DEFAULT 'medium'::character varying NOT NULL,
    priority_tier integer DEFAULT 2 NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    summary text NOT NULL,
    next_steps text,
    context text,
    files jsonb DEFAULT '[]'::jsonb NOT NULL,
    supersedes uuid,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: knowledge; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.knowledge (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title character varying(255) NOT NULL,
    body text NOT NULL,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: lessons; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.lessons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_name character varying(120) NOT NULL,
    summary text NOT NULL,
    detail text,
    quality_score double precision DEFAULT 0.5 NOT NULL,
    times_selected integer DEFAULT 0 NOT NULL,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: messages; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_name character varying(120) NOT NULL,
    role character varying(32) NOT NULL,
    content text NOT NULL,
    tokens integer,
    search_vector tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: pattern_metrics; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.pattern_metrics (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pattern_id uuid NOT NULL,
    outcome character varying(32) NOT NULL,
    duration_ms integer,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: projects; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    display_name character varying(255) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    registered_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    customer_id uuid,
    project_id uuid
);


--
-- Name: promi_maintenance_status; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.promi_maintenance_status (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid,
    project_id uuid,
    task_name character varying(64) NOT NULL,
    last_status character varying(16) DEFAULT 'never_run'::character varying NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    halted boolean DEFAULT false NOT NULL,
    last_started_at timestamp with time zone,
    last_finished_at timestamp with time zone,
    last_error text,
    last_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    run_count integer DEFAULT 0 NOT NULL,
    halted_at timestamp with time zone,
    last_notified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_promi_maintenance_status_failures_nonnegative CHECK ((consecutive_failures >= 0)),
    CONSTRAINT ck_promi_maintenance_status_run_count_nonnegative CHECK ((run_count >= 0)),
    CONSTRAINT ck_promi_maintenance_status_status CHECK (((last_status)::text = ANY ((ARRAY['never_run'::character varying, 'running'::character varying, 'success'::character varying, 'failed'::character varying, 'halted'::character varying])::text[])))
);


--
-- Name: role_audit_events; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.role_audit_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    project_id uuid NOT NULL,
    role_name character varying(120) NOT NULL,
    action character varying(16) NOT NULL,
    actor_scope character varying(32) NOT NULL,
    actor_agent character varying(120),
    actor_user_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_role_audit_events_action CHECK (((action)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'delete'::character varying])::text[])))
);


--
-- Name: roles; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    project_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    display_name character varying(160) NOT NULL,
    description text,
    permissions jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_builtin boolean DEFAULT false NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_roles_name_slug CHECK (((name)::text ~ '^[a-z][a-z0-9_-]{1,119}$'::text)),
    CONSTRAINT ck_roles_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'deleted'::character varying])::text[])))
);


--
-- Name: task_executions; Type: TABLE; Schema: cortex; Owner: -
--

CREATE TABLE cortex.task_executions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    project_id uuid NOT NULL,
    handoff_id uuid,
    agent_name character varying(120),
    state character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    dispatch_source character varying(64) DEFAULT 'promi_active_dispatch'::character varying NOT NULL,
    heartbeat_count integer DEFAULT 0 NOT NULL,
    ping_count integer DEFAULT 0 NOT NULL,
    verify_attempts integer DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone,
    last_heartbeat_at timestamp with time zone,
    completed_at timestamp with time zone,
    failed_at timestamp with time zone,
    last_error text,
    sla_breaches jsonb DEFAULT '[]'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_executions_heartbeat_count_nonnegative CHECK ((heartbeat_count >= 0)),
    CONSTRAINT ck_task_executions_ping_count_nonnegative CHECK ((ping_count >= 0)),
    CONSTRAINT ck_task_executions_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'claimed'::character varying, 'executing'::character varying, 'verifying'::character varying, 'done'::character varying, 'stalled'::character varying, 'escalated'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_task_executions_verify_attempts_nonnegative CHECK ((verify_attempts >= 0))
);


--
-- Name: agent_diaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_diaries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    agent_name text NOT NULL,
    session_id uuid,
    summary text NOT NULL,
    key_decisions uuid[],
    files_modified text[],
    outcome text,
    importance integer DEFAULT 5,
    created_at timestamp with time zone DEFAULT now(),
    metadata jsonb DEFAULT '{}'::jsonb,
    commits text[],
    tests_added integer DEFAULT 0,
    files_touched integer DEFAULT 0,
    regressions_found integer DEFAULT 0,
    handoffs_created uuid[],
    verify_results jsonb DEFAULT '{}'::jsonb,
    project_id uuid,
    actor_id uuid,
    CONSTRAINT agent_diaries_importance_check CHECK (((importance >= 1) AND (importance <= 10))),
    CONSTRAINT agent_diaries_outcome_check CHECK ((outcome = ANY (ARRAY['completed'::text, 'blocked'::text, 'handed-off'::text, 'partial'::text])))
);


--
-- Name: agent_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text DEFAULT '_global'::text NOT NULL,
    agent_name text NOT NULL,
    profile_kind text NOT NULL,
    role text,
    source_file text NOT NULL,
    profile_text text NOT NULL,
    metadata jsonb,
    updated_at timestamp with time zone DEFAULT now(),
    project_id uuid,
    actor_id uuid,
    CONSTRAINT agent_profiles_profile_kind_check CHECK ((profile_kind = ANY (ARRAY['identity'::text, 'role'::text])))
);


--
-- Name: agent_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid,
    sprint_id uuid,
    task text,
    started_at timestamp with time zone DEFAULT now(),
    ended_at timestamp with time zone,
    files_modified text[],
    outcome text,
    handed_off_to uuid,
    notes jsonb,
    project text,
    project_id uuid
);


--
-- Name: agent_skill_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    subject_kind text DEFAULT 'role'::text NOT NULL,
    subject text NOT NULL,
    skill_slug text NOT NULL,
    binding_type text DEFAULT 'include'::text NOT NULL,
    priority integer DEFAULT 50 NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb,
    version_pin text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT agent_skill_bindings_subject_kind_check CHECK ((subject_kind = ANY (ARRAY['role'::text, 'agent'::text])))
);


--
-- Name: agent_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    skill_slug text NOT NULL,
    name text,
    description text,
    skill_type text DEFAULT 'capability'::text NOT NULL,
    scope text DEFAULT 'project'::text NOT NULL,
    permission text,
    body_ref text,
    body_hash text,
    version text DEFAULT '1'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    trust_tier text DEFAULT 'standard'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT agent_skills_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'project'::text, 'agent'::text]))),
    CONSTRAINT agent_skills_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text, 'draft'::text])))
);


--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    project text NOT NULL,
    role text,
    model text,
    capabilities jsonb,
    created_at timestamp with time zone DEFAULT now(),
    status text DEFAULT 'available'::text NOT NULL,
    runtime_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    project_id uuid,
    actor_id uuid
);


--
-- Name: amad_loop_passes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.amad_loop_passes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    epic_id text NOT NULL,
    increment_id text NOT NULL,
    pass_number integer NOT NULL,
    findings jsonb DEFAULT '[]'::jsonb,
    severity text DEFAULT 'none'::text,
    summary text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: archive_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.archive_decisions (
    id uuid NOT NULL,
    sprint_id uuid,
    agent_name text,
    summary text NOT NULL,
    rationale text,
    outcome text,
    category text,
    files_affected text[],
    tags text[],
    project text,
    created_at timestamp with time zone,
    project_id uuid,
    actor_id uuid
);


--
-- Name: archive_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.archive_events (
    id bigint NOT NULL,
    ts timestamp with time zone,
    agent_name text NOT NULL,
    event_type text NOT NULL,
    summary text NOT NULL,
    detail jsonb,
    files text[],
    project text,
    sprint_id uuid,
    project_id uuid,
    actor_id uuid
);


--
-- Name: archive_handoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.archive_handoffs (
    id uuid NOT NULL,
    kind text DEFAULT 'task'::text NOT NULL,
    reply_to_handoff_id uuid,
    project text,
    from_agent text NOT NULL,
    to_role text NOT NULL,
    priority text,
    summary text NOT NULL,
    files_changed text[],
    acceptance jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    completion_report jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    escalation jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text,
    created_at timestamp with time zone,
    returned_at timestamp with time zone,
    completed_at timestamp with time zone,
    project_id uuid,
    from_actor_id uuid,
    CONSTRAINT archive_handoffs_kind_check CHECK ((kind = ANY (ARRAY['task'::text, 'completion_handback'::text])))
);


--
-- Name: archive_lessons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.archive_lessons (
    id uuid NOT NULL,
    agent_name text,
    category text,
    summary text NOT NULL,
    detail text,
    code_right text,
    code_wrong text,
    project text,
    created_at timestamp with time zone,
    project_id uuid,
    actor_id uuid
);


--
-- Name: archive_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.archive_messages (
    id bigint NOT NULL,
    session_id uuid,
    project text,
    agent_name text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    metadata jsonb,
    ts timestamp with time zone,
    project_id uuid,
    actor_id uuid,
    content_zstd bytea,
    retained_until timestamp with time zone,
    raw_session_id uuid
);


--
-- Name: artifact_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifact_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    source_id uuid NOT NULL,
    target_type text NOT NULL,
    target_ref text NOT NULL,
    edge_type text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    customer_id uuid,
    org_id uuid,
    parent_artifact_id uuid,
    modality text,
    source_type text,
    source_file text NOT NULL,
    extraction_method text,
    content_hash text NOT NULL,
    raw_content text,
    section_context text,
    metadata jsonb DEFAULT '{}'::jsonb,
    embedding public.vector(768),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    caption text,
    neighborhood_text text,
    source_doc_metadata jsonb DEFAULT '{}'::jsonb,
    project_id uuid
);


--
-- Name: captured_patterns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.captured_patterns (
    id bigint NOT NULL,
    project text NOT NULL,
    session_id uuid,
    agent_name text NOT NULL,
    pattern_type text,
    title text NOT NULL,
    description text,
    trigger_context text,
    solution text,
    embedding public.vector(768),
    search_vector tsvector,
    times_selected integer DEFAULT 0,
    times_applied integer DEFAULT 0,
    times_completed integer DEFAULT 0,
    times_fallback integer DEFAULT 0,
    quality_score real,
    parent_pattern_id bigint,
    generation integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT captured_patterns_pattern_type_check CHECK ((pattern_type = ANY (ARRAY['debugging'::text, 'deployment'::text, 'architecture'::text, 'workaround'::text, 'testing'::text, 'performance'::text, 'other'::text])))
);


--
-- Name: captured_patterns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.captured_patterns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: captured_patterns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.captured_patterns_id_seq OWNED BY public.captured_patterns.id;


--
-- Name: cortex_actor_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_actor_aliases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    alias_text text NOT NULL,
    alias_kind text DEFAULT 'observed'::text NOT NULL,
    source text DEFAULT 'identity-v2'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cortex_actor_aliases_alias_nonempty CHECK ((btrim(alias_text) <> ''::text)),
    CONSTRAINT ck_cortex_actor_aliases_kind CHECK ((alias_kind = ANY (ARRAY['display'::text, 'bare'::text, 'observed'::text]))),
    CONSTRAINT ck_cortex_actor_aliases_no_colon CHECK ((POSITION((':'::text) IN (alias_text)) = 0))
);


--
-- Name: cortex_actors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_actors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    slug text NOT NULL,
    kind text DEFAULT 'agent'::text NOT NULL,
    status text DEFAULT 'historical'::text NOT NULL,
    display_name text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cortex_actors_kind CHECK ((kind = ANY (ARRAY['agent'::text, 'human'::text, 'system'::text]))),
    CONSTRAINT ck_cortex_actors_slug CHECK (public.cortex_identity_v2_valid_slug(slug)),
    CONSTRAINT ck_cortex_actors_status CHECK ((status = ANY (ARRAY['active'::text, 'historical'::text, 'system'::text, 'retired'::text])))
);


--
-- Name: cortex_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_audit_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    agent_name text NOT NULL,
    project text NOT NULL,
    endpoint text NOT NULL,
    method text NOT NULL,
    status_code integer,
    detail jsonb
);


--
-- Name: cortex_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cortex_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cortex_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cortex_audit_log_id_seq OWNED BY public.cortex_audit_log.id;


--
-- Name: cortex_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_entities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    name text NOT NULL,
    entity_type text NOT NULL,
    properties jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: cortex_projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_key text NOT NULL,
    display_name text NOT NULL,
    parent_project_key text,
    repo_root text NOT NULL,
    repo_type text DEFAULT 'repo'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    default_agent text,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    graph_generation uuid DEFAULT gen_random_uuid() NOT NULL,
    graph_requires_full_rebuild boolean DEFAULT true NOT NULL
);


--
-- Name: decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sprint_id uuid,
    agent_id uuid,
    summary text NOT NULL,
    rationale text,
    outcome text,
    category text,
    files_affected text[],
    tags text[],
    embedding public.vector(768),
    created_at timestamp with time zone DEFAULT now(),
    superseded_by uuid,
    agent_name text,
    project text,
    invalidated_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb,
    times_selected integer DEFAULT 0,
    times_applied integer DEFAULT 0,
    times_completed integer DEFAULT 0,
    times_fallback integer DEFAULT 0,
    quality_score real,
    parent_decision_id uuid,
    generation integer DEFAULT 0,
    supersession_summary text,
    search_vector tsvector,
    parent_goal_id text,
    project_id uuid,
    actor_id uuid,
    compacted boolean DEFAULT false NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01');


--
-- Name: handoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.handoffs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text DEFAULT 'task'::text NOT NULL,
    reply_to_handoff_id uuid,
    project text NOT NULL,
    from_agent text NOT NULL,
    from_role text,
    to_role text NOT NULL,
    priority text DEFAULT 'medium'::text,
    sprint_id uuid,
    branch text,
    summary text NOT NULL,
    files_changed text[],
    verification text,
    next_steps text,
    context text,
    acceptance jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    completion_report jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    escalation jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text,
    claimed_by text,
    claimed_at timestamp with time zone,
    returned_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    invalidated_at timestamp with time zone,
    terminal_reason text,
    to_agent text,
    parent_goal_id text,
    project_id uuid,
    from_actor_id uuid,
    to_actor_id uuid,
    claimed_by_actor_id uuid,
    CONSTRAINT handoffs_kind_check CHECK ((kind = ANY (ARRAY['task'::text, 'completion_handback'::text]))),
    CONSTRAINT handoffs_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'urgent'::text]))),
    CONSTRAINT handoffs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'claimed'::text, 'returned'::text, 'completed'::text, 'released'::text, 'abandoned'::text, 'failed'::text, 'archived'::text])))
);


--
-- Name: COLUMN handoffs.completed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.completed_at IS 'Timestamp the handoff exited the active state, regardless of terminal kind (completed/released/abandoned/failed). Renamed semantically in v2.6.2 but column name preserved for backward compat.';


--
-- Name: COLUMN handoffs.completion_report; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.completion_report IS 'Structured worker outcome, evidence, risks, and follow-up actions.';


--
-- Name: COLUMN handoffs.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.kind IS 'Handoff record type: delegated task or completion handback.';


--
-- Name: COLUMN handoffs.reply_to_handoff_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.reply_to_handoff_id IS 'Original task addressed by a completion handback.';


--
-- Name: COLUMN handoffs.returned_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.returned_at IS 'Timestamp the worker returned the task for delegator review.';


--
-- Name: COLUMN handoffs.terminal_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.handoffs.terminal_reason IS 'Free-form audit text for non-completed terminal transitions: released (claimer dropped it back), abandoned (work no longer needed), failed (claim hit unrecoverable error). NULL for completed.';

COMMENT ON COLUMN public.handoffs.acceptance IS 'Structured acceptance contract for the handoff, for example criteria, required checks, or approver expectations.';

COMMENT ON COLUMN public.handoffs.evidence IS 'Structured evidence requirements/results for completing the handoff.';

COMMENT ON COLUMN public.handoffs.retry IS 'Structured retry policy for failed or rejected execution attempts.';

COMMENT ON COLUMN public.handoffs.escalation IS 'Structured escalation policy for blocked, stale, or failed handoffs.';


--
-- Name: knowledge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    content text NOT NULL,
    source_file text,
    category text,
    section text,
    project text DEFAULT '_global'::text NOT NULL,
    embedding public.vector(768),
    updated_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now(),
    metadata jsonb DEFAULT '{}'::jsonb,
    times_selected integer DEFAULT 0,
    times_applied integer DEFAULT 0,
    times_completed integer DEFAULT 0,
    times_fallback integer DEFAULT 0,
    quality_score real,
    search_vector tsvector,
    project_id uuid,
    CONSTRAINT ck_identity_v2_knowledge_project_id_present CHECK (((project ~~ like_escape('\_%'::text, '\'::text)) OR (project_id IS NOT NULL)))
);


--
-- Name: lessons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lessons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    decision_id uuid,
    agent_id uuid,
    category text,
    summary text NOT NULL,
    detail text,
    code_right text,
    code_wrong text,
    times_referenced integer DEFAULT 0,
    embedding public.vector(768),
    created_at timestamp with time zone DEFAULT now(),
    agent_name text,
    project text,
    invalidated_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb,
    importance integer DEFAULT 5,
    times_selected integer DEFAULT 0,
    times_applied integer DEFAULT 0,
    times_completed integer DEFAULT 0,
    times_fallback integer DEFAULT 0,
    quality_score real,
    search_vector tsvector,
    project_id uuid,
    actor_id uuid
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id bigint NOT NULL,
    session_id uuid,
    project text NOT NULL,
    agent_name text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    metadata jsonb,
    embedding public.vector(768),
    ts timestamp with time zone DEFAULT now(),
    search_vector tsvector,
    project_id uuid,
    actor_id uuid,
    distilled boolean DEFAULT false NOT NULL,
    CONSTRAINT messages_role_check CHECK ((role = ANY (ARRAY['human'::text, 'agent'::text, 'system'::text])))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01');


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    sprint_id uuid,
    title text NOT NULL,
    description text,
    assigned_role text,
    assigned_agent text,
    status text DEFAULT 'todo'::text,
    priority integer DEFAULT 50,
    tags text[],
    blocked_by uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    project_id uuid,
    assigned_actor_id uuid,
    CONSTRAINT tasks_status_check CHECK ((status = ANY (ARRAY['todo'::text, 'in_progress'::text, 'review'::text, 'done'::text, 'blocked'::text])))
);


--
-- Name: team_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_events (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    agent_name text NOT NULL,
    event_type text NOT NULL,
    summary text NOT NULL,
    detail jsonb,
    files text[],
    sprint_id uuid,
    related_decision_id uuid,
    project text NOT NULL,
    project_id uuid,
    actor_id uuid
);


--
-- Name: work_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_products (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    handoff_id uuid,
    agent_name text,
    activity_type text DEFAULT 'task-completed'::text NOT NULL,
    status text DEFAULT 'current'::text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL,
    behavior_summary text,
    architecture_notes text,
    files_changed text[] DEFAULT '{}'::text[],
    symbols_changed text[] DEFAULT '{}'::text[],
    subject_entities text[] DEFAULT '{}'::text[],
    artifact_refs text[] DEFAULT '{}'::text[],
    tests_run jsonb DEFAULT '[]'::jsonb,
    risks text[] DEFAULT '{}'::text[],
    followups text[] DEFAULT '{}'::text[],
    approval_status text,
    content_hash text,
    source_event_id bigint,
    supersedes_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb,
    embedding public.vector(768),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    invalidated_at timestamp with time zone,
    commit_sha text,
    file_hashes jsonb DEFAULT '{}'::jsonb,
    symbol_hashes jsonb DEFAULT '{}'::jsonb,
    freshness_status text DEFAULT 'unknown'::text NOT NULL,
    freshness_reason text,
    freshness_checked_at timestamp with time zone,
    projection_status text DEFAULT 'pending'::text NOT NULL,
    projection_error text,
    projected_at timestamp with time zone,
    valid_from timestamp with time zone DEFAULT now(),
    valid_to timestamp with time zone,
    project_id uuid,
    actor_id uuid
);


--
-- Name: cortex_identity_v2_audit; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.cortex_identity_v2_audit AS
 WITH project_rows(table_name, project, project_id) AS (
         SELECT 'decisions'::text AS "?column?",
            decisions.project,
            decisions.project_id
           FROM public.decisions
        UNION ALL
         SELECT 'lessons'::text,
            lessons.project,
            lessons.project_id
           FROM public.lessons
        UNION ALL
         SELECT 'team_events'::text,
            team_events.project,
            team_events.project_id
           FROM public.team_events
        UNION ALL
         SELECT 'messages'::text,
            messages.project,
            messages.project_id
           FROM public.messages
        UNION ALL
         SELECT 'work_products'::text,
            work_products.project,
            work_products.project_id
           FROM public.work_products
        UNION ALL
         SELECT 'handoffs'::text,
            handoffs.project,
            handoffs.project_id
           FROM public.handoffs
        UNION ALL
         SELECT 'tasks'::text,
            tasks.project,
            tasks.project_id
           FROM public.tasks
        UNION ALL
         SELECT 'knowledge'::text,
            knowledge.project,
            knowledge.project_id
           FROM public.knowledge
        UNION ALL
         SELECT 'artifacts'::text,
            artifacts.project,
            artifacts.project_id
           FROM public.artifacts
        UNION ALL
         SELECT 'cortex_entities'::text,
            cortex_entities.project,
            cortex_entities.project_id
           FROM public.cortex_entities
        ), identity_rows(table_name, project, column_name, identity_value, actor_id) AS (
         SELECT 'decisions'::text AS "?column?",
            decisions.project,
            'agent_name'::text AS "?column?",
            decisions.agent_name,
            decisions.actor_id
           FROM public.decisions
          WHERE (decisions.agent_name IS NOT NULL)
        UNION ALL
         SELECT 'lessons'::text,
            lessons.project,
            'agent_name'::text,
            lessons.agent_name,
            lessons.actor_id
           FROM public.lessons
          WHERE (lessons.agent_name IS NOT NULL)
        UNION ALL
         SELECT 'team_events'::text,
            team_events.project,
            'agent_name'::text,
            team_events.agent_name,
            team_events.actor_id
           FROM public.team_events
          WHERE (team_events.agent_name IS NOT NULL)
        UNION ALL
         SELECT 'messages'::text,
            messages.project,
            'agent_name'::text,
            messages.agent_name,
            messages.actor_id
           FROM public.messages
          WHERE (messages.agent_name IS NOT NULL)
        UNION ALL
         SELECT 'work_products'::text,
            work_products.project,
            'agent_name'::text,
            work_products.agent_name,
            work_products.actor_id
           FROM public.work_products
          WHERE (work_products.agent_name IS NOT NULL)
        UNION ALL
         SELECT 'handoffs'::text,
            handoffs.project,
            'from_agent'::text,
            handoffs.from_agent,
            handoffs.from_actor_id
           FROM public.handoffs
          WHERE (handoffs.from_agent IS NOT NULL)
        UNION ALL
         SELECT 'handoffs'::text,
            handoffs.project,
            'to_agent'::text,
            handoffs.to_agent,
            handoffs.to_actor_id
           FROM public.handoffs
          WHERE (handoffs.to_agent IS NOT NULL)
        UNION ALL
         SELECT 'handoffs'::text,
            handoffs.project,
            'claimed_by'::text,
            handoffs.claimed_by,
            handoffs.claimed_by_actor_id
           FROM public.handoffs
          WHERE (handoffs.claimed_by IS NOT NULL)
        UNION ALL
         SELECT 'tasks'::text,
            tasks.project,
            'assigned_agent'::text,
            tasks.assigned_agent,
            tasks.assigned_actor_id
           FROM public.tasks
          WHERE (tasks.assigned_agent IS NOT NULL)
        ), alias_rows(table_name, project, column_name, identity_value, actor_id) AS (
         SELECT 'cortex_actor_aliases'::text AS "?column?",
            cp.project_key,
            'alias_text'::text AS "?column?",
            ca.alias_text,
            ca.actor_id
           FROM (public.cortex_actor_aliases ca
             JOIN public.cortex_projects cp ON ((cp.id = ca.project_id)))
        )
 SELECT project_rows.table_name,
    project_rows.project,
    NULL::text AS column_name,
    NULL::text AS identity_value,
    'missing_project_id'::text AS issue,
    count(*) AS row_count
   FROM project_rows
  WHERE ((project_rows.project IS NOT NULL) AND (lower(btrim(project_rows.project)) <> ALL (ARRAY['_global'::text, '_local_state'::text])) AND (project_rows.project_id IS NULL))
  GROUP BY project_rows.table_name, project_rows.project
UNION ALL
 SELECT identity_rows.table_name,
    identity_rows.project,
    identity_rows.column_name,
    identity_rows.identity_value,
    'legacy_colon_identity'::text AS issue,
    count(*) AS row_count
   FROM identity_rows
  WHERE (identity_rows.identity_value ~ '^[a-z][a-z0-9_-]*:'::text)
  GROUP BY identity_rows.table_name, identity_rows.project, identity_rows.column_name, identity_rows.identity_value
UNION ALL
 SELECT identity_rows.table_name,
    identity_rows.project,
    identity_rows.column_name,
    identity_rows.identity_value,
    'invalid_actor_slug'::text AS issue,
    count(*) AS row_count
   FROM identity_rows
  WHERE (NOT public.cortex_identity_v2_valid_slug(public.cortex_identity_base(identity_rows.identity_value)))
  GROUP BY identity_rows.table_name, identity_rows.project, identity_rows.column_name, identity_rows.identity_value
UNION ALL
 SELECT identity_rows.table_name,
    identity_rows.project,
    identity_rows.column_name,
    identity_rows.identity_value,
    'missing_actor_id'::text AS issue,
    count(*) AS row_count
   FROM identity_rows
  WHERE (public.cortex_identity_v2_valid_slug(public.cortex_identity_base(identity_rows.identity_value)) AND (identity_rows.actor_id IS NULL))
  GROUP BY identity_rows.table_name, identity_rows.project, identity_rows.column_name, identity_rows.identity_value
UNION ALL
 SELECT alias_rows.table_name,
    alias_rows.project,
    alias_rows.column_name,
    alias_rows.identity_value,
    'legacy_alias_identity'::text AS issue,
    count(*) AS row_count
   FROM alias_rows
  WHERE (alias_rows.identity_value ~ '^[a-z][a-z0-9_-]*:'::text)
  GROUP BY alias_rows.table_name, alias_rows.project, alias_rows.column_name, alias_rows.identity_value;


--
-- Name: cortex_identity_v2_audit_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.cortex_identity_v2_audit_summary AS
 SELECT issue,
    table_name,
    project,
    (sum(row_count))::bigint AS row_count
   FROM public.cortex_identity_v2_audit
  GROUP BY issue, table_name, project
  ORDER BY issue, table_name, project;


--
-- Name: cortex_legacy_identity_archive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_legacy_identity_archive (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    archived_at timestamp with time zone DEFAULT now() NOT NULL,
    archive_reason text NOT NULL,
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_pk text,
    project_key text,
    project_id uuid,
    legacy_project_hex text,
    legacy_customer_hex text,
    legacy_identity text,
    legacy_payload jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: cortex_meta; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_meta (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: cortex_project_paths; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_project_paths (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_key text NOT NULL,
    root_path text NOT NULL,
    path_kind text DEFAULT 'primary'::text NOT NULL,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: cortex_relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_relationships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    source_entity_id uuid,
    target_entity_id uuid,
    relationship_type text NOT NULL,
    properties jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: cortex_schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_schema_migrations (
    migration_id text NOT NULL,
    checksum_sha256 text NOT NULL,
    source_path text NOT NULL,
    applied_by text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    statement_status text,
    surface_version text
);


--
-- Name: cortex_platform_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cortex_platform_config (
    id boolean DEFAULT true NOT NULL,
    embedding_provider text DEFAULT 'openrouter'::text NOT NULL,
    embedding_model text DEFAULT 'nvidia/llama-nemotron-embed-vl-1b-v2:free'::text NOT NULL,
    embedding_dims integer DEFAULT 768 NOT NULL,
    rerank_enabled boolean DEFAULT true NOT NULL,
    rerank_provider text DEFAULT 'nvidia'::text NOT NULL,
    rerank_model text DEFAULT 'nv-rerank-qa-mistral-4b:1'::text NOT NULL,
    analysis_provider text DEFAULT 'openrouter'::text NOT NULL,
    analysis_model text DEFAULT 'google/gemma-4-31b-it:free'::text NOT NULL,
    cortex_api_url text DEFAULT 'http://localhost:8501'::text NOT NULL,
    boot_context_version text DEFAULT 'v2'::text NOT NULL,
    max_boot_tokens integer DEFAULT 250 NOT NULL,
    search_confidence_threshold double precision DEFAULT 0.015 NOT NULL,
    rrf_k integer DEFAULT 60 NOT NULL,
    embed_input_max_chars integer DEFAULT 500 NOT NULL,
    rerank_input_max_chars integer DEFAULT 500 NOT NULL,
    embed_timeout_ms integer DEFAULT 15000 NOT NULL,
    rerank_timeout_ms integer DEFAULT 15000 NOT NULL,
    analysis_timeout_ms integer DEFAULT 90000 NOT NULL,
    embedding_provider_config_id uuid,
    rerank_provider_config_id uuid,
    analysis_provider_config_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cortex_platform_config_id_check CHECK (id),
    CONSTRAINT cortex_platform_config_embedding_dims_check CHECK ((embedding_dims > 0)),
    CONSTRAINT cortex_platform_config_max_boot_tokens_check CHECK ((max_boot_tokens > 0)),
    CONSTRAINT cortex_platform_config_rrf_k_check CHECK ((rrf_k > 0)),
    CONSTRAINT cortex_platform_config_embed_input_max_chars_check CHECK ((embed_input_max_chars > 0)),
    CONSTRAINT cortex_platform_config_rerank_input_max_chars_check CHECK ((rerank_input_max_chars > 0)),
    CONSTRAINT cortex_platform_config_embed_timeout_ms_check CHECK ((embed_timeout_ms > 0)),
    CONSTRAINT cortex_platform_config_rerank_timeout_ms_check CHECK ((rerank_timeout_ms > 0)),
    CONSTRAINT cortex_platform_config_analysis_timeout_ms_check CHECK ((analysis_timeout_ms > 0))
);


--
-- Name: embedding_backfill_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embedding_backfill_jobs (
    id uuid NOT NULL,
    project text NOT NULL,
    table_name text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    limit_requested integer DEFAULT 100 NOT NULL,
    chunk_size integer DEFAULT 100 NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    max_errors integer DEFAULT 10 NOT NULL,
    error_threshold integer DEFAULT 3 NOT NULL,
    provider_configured boolean DEFAULT false NOT NULL,
    processed integer DEFAULT 0 NOT NULL,
    embedded integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL,
    skipped integer DEFAULT 0 NOT NULL,
    stopped text DEFAULT ''::text NOT NULL,
    tables jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT embedding_backfill_jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: graph_build_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_build_jobs (
    id uuid NOT NULL,
    project text NOT NULL,
    repo text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    full_rebuild boolean DEFAULT false NOT NULL,
    embed boolean DEFAULT true NOT NULL,
    request jsonb DEFAULT '{}'::jsonb NOT NULL,
    result jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT graph_build_jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: epics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.epics (
    project text NOT NULL,
    epic_id text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    overall_pct integer DEFAULT 0 NOT NULL,
    increments jsonb DEFAULT '[]'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT epics_overall_pct_check CHECK (((overall_pct >= 0) AND (overall_pct <= 100)))
);


--
-- Name: execution_analyses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_analyses (
    id bigint NOT NULL,
    project text NOT NULL,
    session_id uuid NOT NULL,
    agent_name text NOT NULL,
    task_completed boolean,
    quality_score real,
    patterns_used text[] DEFAULT '{}'::text[],
    patterns_failed text[] DEFAULT '{}'::text[],
    novel_patterns text[] DEFAULT '{}'::text[],
    tools_used jsonb DEFAULT '[]'::jsonb,
    summary text,
    raw_analysis jsonb,
    embedding public.vector(768),
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT execution_analyses_quality_score_check CHECK (((quality_score >= (0)::double precision) AND (quality_score <= (10)::double precision)))
);


--
-- Name: execution_analyses_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.execution_analyses_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: execution_analyses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.execution_analyses_id_seq OWNED BY public.execution_analyses.id;


--
-- Name: harness_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.harness_artifacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    harness text NOT NULL,
    path text NOT NULL,
    generated_from_hash text NOT NULL,
    last_compiled_at timestamp with time zone NOT NULL,
    status text NOT NULL,
    project_id uuid NOT NULL,
    CONSTRAINT harness_artifacts_status_check CHECK ((status = ANY (ARRAY['current'::text, 'stale'::text, 'error'::text])))
);


--
-- Name: memory_sync_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_sync_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text NOT NULL,
    target text NOT NULL,
    direction text NOT NULL,
    content_hash text NOT NULL,
    conflict_policy text NOT NULL,
    result text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid NOT NULL,
    CONSTRAINT memory_sync_events_direction_check CHECK ((direction = ANY (ARRAY['to-cortex'::text, 'to-harness'::text]))),
    CONSTRAINT memory_sync_events_result_check CHECK ((result = ANY (ARRAY['accepted'::text, 'rejected'::text, 'inbox'::text])))
);


--
-- Name: messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.messages_id_seq OWNED BY public.messages.id;


--
-- Name: pattern_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pattern_metrics (
    id bigint NOT NULL,
    project text NOT NULL,
    pattern_key text NOT NULL,
    pattern_type text,
    total_uses integer DEFAULT 0,
    successes integer DEFAULT 0,
    failures integer DEFAULT 0,
    consecutive_failures integer DEFAULT 0,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    degraded boolean DEFAULT false,
    degradation_surfaced boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT pattern_metrics_pattern_type_check CHECK ((pattern_type = ANY (ARRAY['command'::text, 'deploy'::text, 'test'::text, 'api_call'::text, 'build'::text, 'migration'::text, 'other'::text])))
);


--
-- Name: pattern_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pattern_metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pattern_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pattern_metrics_id_seq OWNED BY public.pattern_metrics.id;


--
-- Name: profile_bundles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.profile_bundles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_name text NOT NULL,
    version integer NOT NULL,
    content_hash text NOT NULL,
    source_paths jsonb NOT NULL,
    rendered_markdown text NOT NULL,
    rendered_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid NOT NULL
);


--
-- Name: retention_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.retention_config (
    table_name text NOT NULL,
    tier2_days integer DEFAULT 90 NOT NULL,
    description text
);


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    project text NOT NULL,
    name text NOT NULL,
    default_capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    is_builtin boolean DEFAULT false NOT NULL,
    source_file text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    rule_slug text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    source_file text,
    version text DEFAULT '1'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT rules_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text, 'draft'::text])))
);


--
-- Name: session_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_sources (
    session_id uuid NOT NULL,
    project text NOT NULL,
    source_path text NOT NULL,
    provider text NOT NULL,
    agent_name text NOT NULL,
    cwd text,
    git_branch text,
    source_kind text,
    metadata jsonb,
    ingested_at timestamp with time zone DEFAULT now()
);


--
-- Name: sprints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sprints (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sprint_number integer,
    sprint_label text,
    project text NOT NULL,
    goal text NOT NULL,
    status text DEFAULT 'active'::text,
    started_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone,
    retrospective jsonb
);


--
-- Name: team_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.team_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: team_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.team_events_id_seq OWNED BY public.team_events.id;


--
-- Name: captured_patterns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.captured_patterns ALTER COLUMN id SET DEFAULT nextval('public.captured_patterns_id_seq'::regclass);


--
-- Name: cortex_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_audit_log ALTER COLUMN id SET DEFAULT nextval('public.cortex_audit_log_id_seq'::regclass);


--
-- Name: execution_analyses id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_analyses ALTER COLUMN id SET DEFAULT nextval('public.execution_analyses_id_seq'::regclass);


--
-- Name: messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages ALTER COLUMN id SET DEFAULT nextval('public.messages_id_seq'::regclass);


--
-- Name: pattern_metrics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pattern_metrics ALTER COLUMN id SET DEFAULT nextval('public.pattern_metrics_id_seq'::regclass);


--
-- Name: team_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events ALTER COLUMN id SET DEFAULT nextval('public.team_events_id_seq'::regclass);


--
-- Name: agent_diaries agent_diaries_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.agent_diaries
    ADD CONSTRAINT agent_diaries_pkey PRIMARY KEY (id);


--
-- Name: agent_knowledge_sources agent_knowledge_sources_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.agent_knowledge_sources
    ADD CONSTRAINT agent_knowledge_sources_pkey PRIMARY KEY (id);


--
-- Name: agent_profiles agent_profiles_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.agent_profiles
    ADD CONSTRAINT agent_profiles_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: analysis_cost_log analysis_cost_log_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.analysis_cost_log
    ADD CONSTRAINT analysis_cost_log_pkey PRIMARY KEY (id);


--
-- Name: artifact_edges artifact_edges_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.artifact_edges
    ADD CONSTRAINT artifact_edges_pkey PRIMARY KEY (id);


--
-- Name: artifacts artifacts_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (id);


--
-- Name: captured_patterns captured_patterns_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.captured_patterns
    ADD CONSTRAINT captured_patterns_pkey PRIMARY KEY (id);


--
-- Name: cortex_cost_log cortex_cost_log_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_cost_log
    ADD CONSTRAINT cortex_cost_log_pkey PRIMARY KEY (id);


--
-- Name: cortex_entities cortex_entities_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_entities
    ADD CONSTRAINT cortex_entities_pkey PRIMARY KEY (id);


--
-- Name: cortex_relationships cortex_relationships_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_relationships
    ADD CONSTRAINT cortex_relationships_pkey PRIMARY KEY (id);


--
-- Name: cortex_tenant_quotas cortex_tenant_quotas_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_tenant_quotas
    ADD CONSTRAINT cortex_tenant_quotas_pkey PRIMARY KEY (id);


--
-- Name: decisions decisions_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.decisions
    ADD CONSTRAINT decisions_pkey PRIMARY KEY (id);


--
-- Name: execution_analyses execution_analyses_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.execution_analyses
    ADD CONSTRAINT execution_analyses_pkey PRIMARY KEY (id);


--
-- Name: handoffs handoffs_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.handoffs
    ADD CONSTRAINT handoffs_pkey PRIMARY KEY (id);


--
-- Name: knowledge knowledge_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.knowledge
    ADD CONSTRAINT knowledge_pkey PRIMARY KEY (id);


--
-- Name: lessons lessons_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.lessons
    ADD CONSTRAINT lessons_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: pattern_metrics pattern_metrics_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.pattern_metrics
    ADD CONSTRAINT pattern_metrics_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: promi_maintenance_status promi_maintenance_status_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.promi_maintenance_status
    ADD CONSTRAINT promi_maintenance_status_pkey PRIMARY KEY (id);


--
-- Name: role_audit_events role_audit_events_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.role_audit_events
    ADD CONSTRAINT role_audit_events_pkey PRIMARY KEY (id);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: task_executions task_executions_pkey; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.task_executions
    ADD CONSTRAINT task_executions_pkey PRIMARY KEY (id);


--
-- Name: promi_maintenance_status ux_promi_maintenance_status_tenant_task; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.promi_maintenance_status
    ADD CONSTRAINT ux_promi_maintenance_status_tenant_task UNIQUE (customer_id, project_id, task_name);


--
-- Name: roles ux_roles_tenant_name; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.roles
    ADD CONSTRAINT ux_roles_tenant_name UNIQUE (customer_id, project_id, name);


--
-- Name: task_executions ux_task_executions_handoff; Type: CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.task_executions
    ADD CONSTRAINT ux_task_executions_handoff UNIQUE (handoff_id);


--
-- Name: agent_diaries agent_diaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_diaries
    ADD CONSTRAINT agent_diaries_pkey PRIMARY KEY (id);


--
-- Name: agent_profiles agent_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_pkey PRIMARY KEY (id);


--
-- Name: agent_sessions agent_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_bindings agent_skill_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_bindings
    ADD CONSTRAINT agent_skill_bindings_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_bindings agent_skill_bindings_project_subject_kind_subject_skill_slu_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_bindings
    ADD CONSTRAINT agent_skill_bindings_project_subject_kind_subject_skill_slu_key UNIQUE (project, subject_kind, subject, skill_slug);


--
-- Name: agent_skills agent_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_pkey PRIMARY KEY (id);


--
-- Name: agent_skills agent_skills_project_skill_slug_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_project_skill_slug_version_key UNIQUE (project, skill_slug, version);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: amad_loop_passes amad_loop_passes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.amad_loop_passes
    ADD CONSTRAINT amad_loop_passes_pkey PRIMARY KEY (id);


--
-- Name: archive_decisions archive_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_decisions
    ADD CONSTRAINT archive_decisions_pkey PRIMARY KEY (id);


--
-- Name: archive_events archive_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_events
    ADD CONSTRAINT archive_events_pkey PRIMARY KEY (id);


--
-- Name: archive_handoffs archive_handoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_handoffs
    ADD CONSTRAINT archive_handoffs_pkey PRIMARY KEY (id);


--
-- Name: archive_lessons archive_lessons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_lessons
    ADD CONSTRAINT archive_lessons_pkey PRIMARY KEY (id);


--
-- Name: archive_messages archive_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_messages
    ADD CONSTRAINT archive_messages_pkey PRIMARY KEY (id);


--
-- Name: artifact_edges artifact_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_edges
    ADD CONSTRAINT artifact_edges_pkey PRIMARY KEY (id);


--
-- Name: artifact_edges artifact_edges_project_source_id_target_type_target_ref_edg_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_edges
    ADD CONSTRAINT artifact_edges_project_source_id_target_type_target_ref_edg_key UNIQUE (project, source_id, target_type, target_ref, edge_type);


--
-- Name: artifacts artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (id);


--
-- Name: artifacts artifacts_project_source_file_content_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_project_source_file_content_hash_key UNIQUE (project, source_file, content_hash);


--
-- Name: captured_patterns captured_patterns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.captured_patterns
    ADD CONSTRAINT captured_patterns_pkey PRIMARY KEY (id);


--
-- Name: cortex_actor_aliases cortex_actor_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_actor_aliases
    ADD CONSTRAINT cortex_actor_aliases_pkey PRIMARY KEY (id);


--
-- Name: cortex_actors cortex_actors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_actors
    ADD CONSTRAINT cortex_actors_pkey PRIMARY KEY (id);


--
-- Name: cortex_audit_log cortex_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_audit_log
    ADD CONSTRAINT cortex_audit_log_pkey PRIMARY KEY (id);


--
-- Name: cortex_entities cortex_entities_natural_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_entities
    ADD CONSTRAINT cortex_entities_natural_key UNIQUE (project, name, entity_type);


--
-- Name: cortex_entities cortex_entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_entities
    ADD CONSTRAINT cortex_entities_pkey PRIMARY KEY (id);


--
-- Name: cortex_legacy_identity_archive cortex_legacy_identity_archive_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_legacy_identity_archive
    ADD CONSTRAINT cortex_legacy_identity_archive_pkey PRIMARY KEY (id);


--
-- Name: cortex_meta cortex_meta_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_meta
    ADD CONSTRAINT cortex_meta_pkey PRIMARY KEY (key);


--
-- Name: cortex_project_paths cortex_project_paths_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_project_paths
    ADD CONSTRAINT cortex_project_paths_pkey PRIMARY KEY (id);


--
-- Name: cortex_project_paths cortex_project_paths_root_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_project_paths
    ADD CONSTRAINT cortex_project_paths_root_path_key UNIQUE (root_path);


--
-- Name: cortex_projects cortex_projects_graph_generation_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_projects
    ADD CONSTRAINT cortex_projects_graph_generation_key UNIQUE (graph_generation);


--
-- Name: cortex_projects cortex_projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_projects
    ADD CONSTRAINT cortex_projects_pkey PRIMARY KEY (id);


--
-- Name: cortex_projects cortex_projects_project_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_projects
    ADD CONSTRAINT cortex_projects_project_key_key UNIQUE (project_key);


--
-- Name: cortex_relationships cortex_relationships_natural_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_relationships
    ADD CONSTRAINT cortex_relationships_natural_key UNIQUE (project, source_entity_id, target_entity_id, relationship_type);


--
-- Name: cortex_relationships cortex_relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_relationships
    ADD CONSTRAINT cortex_relationships_pkey PRIMARY KEY (id);


--
-- Name: cortex_schema_migrations cortex_schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_schema_migrations
    ADD CONSTRAINT cortex_schema_migrations_pkey PRIMARY KEY (migration_id);


--
-- Name: cortex_platform_config cortex_platform_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_platform_config
    ADD CONSTRAINT cortex_platform_config_pkey PRIMARY KEY (id);


--
-- Name: decisions decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT decisions_pkey PRIMARY KEY (id);


--
-- Name: embedding_backfill_jobs embedding_backfill_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_backfill_jobs
    ADD CONSTRAINT embedding_backfill_jobs_pkey PRIMARY KEY (id);


--
-- Name: graph_build_jobs graph_build_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_build_jobs
    ADD CONSTRAINT graph_build_jobs_pkey PRIMARY KEY (id);


--
-- Name: epics epics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.epics
    ADD CONSTRAINT epics_pkey PRIMARY KEY (project, epic_id);


--
-- Name: execution_analyses execution_analyses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_analyses
    ADD CONSTRAINT execution_analyses_pkey PRIMARY KEY (id);


--
-- Name: handoffs handoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT handoffs_pkey PRIMARY KEY (id);


--
-- Name: harness_artifacts harness_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.harness_artifacts
    ADD CONSTRAINT harness_artifacts_pkey PRIMARY KEY (id);


--
-- Name: knowledge knowledge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge
    ADD CONSTRAINT knowledge_pkey PRIMARY KEY (id);


--
-- Name: lessons lessons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_pkey PRIMARY KEY (id);


--
-- Name: memory_sync_events memory_sync_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_sync_events
    ADD CONSTRAINT memory_sync_events_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: pattern_metrics pattern_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pattern_metrics
    ADD CONSTRAINT pattern_metrics_pkey PRIMARY KEY (id);


--
-- Name: pattern_metrics pattern_metrics_project_pattern_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pattern_metrics
    ADD CONSTRAINT pattern_metrics_project_pattern_key_key UNIQUE (project, pattern_key);


--
-- Name: profile_bundles profile_bundles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_bundles
    ADD CONSTRAINT profile_bundles_pkey PRIMARY KEY (id);


--
-- Name: retention_config retention_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retention_config
    ADD CONSTRAINT retention_config_pkey PRIMARY KEY (table_name);


--
-- Data for Name: retention_config; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.retention_config (table_name, tier2_days, description) VALUES
    ('messages', 90, 'Chat history — 90 days in pgvector, then archive'),
    ('team_events', 90, 'Team events — 90 days in pgvector, then archive'),
    ('decisions', 365, 'Decisions — 1 year in pgvector, then archive'),
    ('lessons', 365, 'Lessons — 1 year in pgvector, then archive'),
    ('handoffs', 30, 'Handoffs — 30 days in pgvector, then archive')
ON CONFLICT (table_name) DO NOTHING;


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (project, name);


--
-- Name: rules rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rules
    ADD CONSTRAINT rules_pkey PRIMARY KEY (id);


--
-- Name: rules rules_project_rule_slug_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rules
    ADD CONSTRAINT rules_project_rule_slug_version_key UNIQUE (project, rule_slug, version);


--
-- Name: session_sources session_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_sources
    ADD CONSTRAINT session_sources_pkey PRIMARY KEY (session_id);


--
-- Name: session_sources session_sources_source_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_sources
    ADD CONSTRAINT session_sources_source_path_key UNIQUE (source_path);


--
-- Name: sprints sprints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sprints
    ADD CONSTRAINT sprints_pkey PRIMARY KEY (id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: team_events team_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events
    ADD CONSTRAINT team_events_pkey PRIMARY KEY (id);


--
-- Name: work_products work_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_products
    ADD CONSTRAINT work_products_pkey PRIMARY KEY (id);


--
-- Name: ix_agent_diaries_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_agent_diaries_search ON cortex.agent_diaries USING gin (search_vector);


--
-- Name: ix_agent_diaries_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_agent_diaries_tenant_uuid ON cortex.agent_diaries USING btree (customer_id, project_id);


--
-- Name: ix_agent_knowledge_sources_agent; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_agent_knowledge_sources_agent ON cortex.agent_knowledge_sources USING btree (agent_id);


--
-- Name: ix_agent_knowledge_sources_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_agent_knowledge_sources_tenant_uuid ON cortex.agent_knowledge_sources USING btree (customer_id, project_id);


--
-- Name: ix_agent_profiles_project_name_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ix_agent_profiles_project_name_uuid ON cortex.agent_profiles USING btree (project_id, name);


--
-- Name: ix_agent_profiles_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_agent_profiles_tenant_uuid ON cortex.agent_profiles USING btree (customer_id, project_id);


--
-- Name: ix_analysis_cost_log_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_analysis_cost_log_tenant_uuid ON cortex.analysis_cost_log USING btree (customer_id, project_id);


--
-- Name: ix_artifact_edges_source; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifact_edges_source ON cortex.artifact_edges USING btree (source_id);


--
-- Name: ix_artifact_edges_target; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifact_edges_target ON cortex.artifact_edges USING btree (target_id, target_type);


--
-- Name: ix_artifact_edges_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifact_edges_tenant_uuid ON cortex.artifact_edges USING btree (customer_id, project_id);


--
-- Name: ix_artifacts_caption_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_caption_trgm ON cortex.artifacts USING gin (caption public.gin_trgm_ops);


--
-- Name: ix_artifacts_content_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_content_trgm ON cortex.artifacts USING gin (content public.gin_trgm_ops);


--
-- Name: ix_artifacts_neighborhood_text_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_neighborhood_text_trgm ON cortex.artifacts USING gin (neighborhood_text public.gin_trgm_ops);


--
-- Name: ix_artifacts_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_scope ON cortex.artifacts USING gin (scope_metadata);


--
-- Name: ix_artifacts_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_search ON cortex.artifacts USING gin (search_vector);


--
-- Name: ix_artifacts_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_artifacts_tenant_uuid ON cortex.artifacts USING btree (customer_id, project_id);


--
-- Name: ix_captured_patterns_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_captured_patterns_scope ON cortex.captured_patterns USING gin (scope_metadata);


--
-- Name: ix_captured_patterns_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_captured_patterns_search ON cortex.captured_patterns USING gin (search_vector);


--
-- Name: ix_captured_patterns_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_captured_patterns_tenant_uuid ON cortex.captured_patterns USING btree (customer_id, project_id);


--
-- Name: ix_cortex_cost_log_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_cost_log_tenant_uuid ON cortex.cortex_cost_log USING btree (customer_id, project_id);


--
-- Name: ix_cortex_cost_log_tenant_uuid_ts; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_cost_log_tenant_uuid_ts ON cortex.cortex_cost_log USING btree (customer_id, project_id, created_at);


--
-- Name: ix_cortex_entities_description_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_entities_description_trgm ON cortex.cortex_entities USING gin (lower(COALESCE(description, (metadata ->> 'description'::text), ''::text)) public.gin_trgm_ops);


--
-- Name: ix_cortex_entities_name_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_entities_name_trgm ON cortex.cortex_entities USING gin (lower((name)::text) public.gin_trgm_ops);


--
-- Name: ix_cortex_entities_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_entities_tenant_uuid ON cortex.cortex_entities USING btree (customer_id, project_id);


--
-- Name: ix_cortex_relationships_relationship_type; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_relationships_relationship_type ON cortex.cortex_relationships USING btree (relationship_type);


--
-- Name: ix_cortex_relationships_source; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_relationships_source ON cortex.cortex_relationships USING btree (source_entity_id);


--
-- Name: ix_cortex_relationships_target; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_relationships_target ON cortex.cortex_relationships USING btree (target_entity_id);


--
-- Name: ix_cortex_relationships_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_relationships_tenant_uuid ON cortex.cortex_relationships USING btree (customer_id, project_id);


--
-- Name: ix_cortex_tenant_quotas_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_cortex_tenant_quotas_tenant_uuid ON cortex.cortex_tenant_quotas USING btree (customer_id, project_id);


--
-- Name: ix_decisions_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_decisions_scope ON cortex.decisions USING gin (scope_metadata);


--
-- Name: ix_decisions_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_decisions_search ON cortex.decisions USING gin (search_vector);


--
-- Name: ix_decisions_summary_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_decisions_summary_trgm ON cortex.decisions USING gin (summary public.gin_trgm_ops);


--
-- Name: ix_decisions_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_decisions_tenant_uuid ON cortex.decisions USING btree (customer_id, project_id);


--
-- Name: ix_execution_analyses_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_execution_analyses_search ON cortex.execution_analyses USING gin (search_vector);


--
-- Name: ix_execution_analyses_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_execution_analyses_tenant_uuid ON cortex.execution_analyses USING btree (customer_id, project_id);


--
-- Name: ix_handoffs_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_handoffs_scope ON cortex.handoffs USING gin (scope_metadata);


--
-- Name: ix_handoffs_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_handoffs_search ON cortex.handoffs USING gin (search_vector);


--
-- Name: ix_handoffs_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_handoffs_tenant_uuid ON cortex.handoffs USING btree (customer_id, project_id);


--
-- Name: ix_knowledge_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_knowledge_scope ON cortex.knowledge USING gin (scope_metadata);


--
-- Name: ix_knowledge_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_knowledge_search ON cortex.knowledge USING gin (search_vector);


--
-- Name: ix_knowledge_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_knowledge_tenant_uuid ON cortex.knowledge USING btree (customer_id, project_id);


--
-- Name: ix_knowledge_title_trgm; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_knowledge_title_trgm ON cortex.knowledge USING gin (title public.gin_trgm_ops);


--
-- Name: ix_lessons_scope; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_lessons_scope ON cortex.lessons USING gin (scope_metadata);


--
-- Name: ix_lessons_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_lessons_search ON cortex.lessons USING gin (search_vector);


--
-- Name: ix_lessons_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_lessons_tenant_uuid ON cortex.lessons USING btree (customer_id, project_id);


--
-- Name: ix_messages_search; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_messages_search ON cortex.messages USING gin (search_vector);


--
-- Name: ix_messages_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_messages_tenant_uuid ON cortex.messages USING btree (customer_id, project_id);


--
-- Name: ix_pattern_metrics_pattern; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_pattern_metrics_pattern ON cortex.pattern_metrics USING btree (pattern_id);


--
-- Name: ix_pattern_metrics_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_pattern_metrics_tenant_uuid ON cortex.pattern_metrics USING btree (customer_id, project_id);


--
-- Name: ix_projects_customer_project_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ix_projects_customer_project_uuid ON cortex.projects USING btree (customer_id, project_id) WHERE ((customer_id IS NOT NULL) AND (project_id IS NOT NULL));


--
-- Name: ix_projects_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_projects_tenant_uuid ON cortex.projects USING btree (customer_id, project_id);


--
-- Name: ix_promi_maintenance_status_halted; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_promi_maintenance_status_halted ON cortex.promi_maintenance_status USING btree (halted, updated_at);


--
-- Name: ix_promi_maintenance_status_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_promi_maintenance_status_tenant_uuid ON cortex.promi_maintenance_status USING btree (customer_id, project_id);


--
-- Name: ix_role_audit_events_tenant_created; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_role_audit_events_tenant_created ON cortex.role_audit_events USING btree (customer_id, project_id, created_at);


--
-- Name: ix_role_audit_events_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_role_audit_events_tenant_uuid ON cortex.role_audit_events USING btree (customer_id, project_id);


--
-- Name: ix_roles_builtin; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_roles_builtin ON cortex.roles USING btree (is_builtin, status);


--
-- Name: ix_roles_tenant_status; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_roles_tenant_status ON cortex.roles USING btree (customer_id, project_id, status);


--
-- Name: ix_roles_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_roles_tenant_uuid ON cortex.roles USING btree (customer_id, project_id);


--
-- Name: ix_task_executions_active; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_task_executions_active ON cortex.task_executions USING btree (customer_id, project_id, updated_at) WHERE ((state)::text = ANY ((ARRAY['pending'::character varying, 'claimed'::character varying, 'executing'::character varying, 'verifying'::character varying, 'stalled'::character varying])::text[]));


--
-- Name: ix_task_executions_agent_state; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_task_executions_agent_state ON cortex.task_executions USING btree (agent_name, state, updated_at);


--
-- Name: ix_task_executions_heartbeat; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_task_executions_heartbeat ON cortex.task_executions USING btree (last_heartbeat_at) WHERE ((state)::text = ANY ((ARRAY['claimed'::character varying, 'executing'::character varying])::text[]));


--
-- Name: ix_task_executions_tenant_state; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_task_executions_tenant_state ON cortex.task_executions USING btree (customer_id, project_id, state, updated_at);


--
-- Name: ix_task_executions_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE INDEX ix_task_executions_tenant_uuid ON cortex.task_executions USING btree (customer_id, project_id);


--
-- Name: ux_artifact_edges_project_identity; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ux_artifact_edges_project_identity ON cortex.artifact_edges USING btree (project_id, source_id, target_id, edge_type);


--
-- Name: ux_artifacts_project_source_ref; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ux_artifacts_project_source_ref ON cortex.artifacts USING btree (project_id, source_ref) WHERE (source_ref IS NOT NULL);


--
-- Name: ux_cortex_tenant_quotas_tenant_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ux_cortex_tenant_quotas_tenant_uuid ON cortex.cortex_tenant_quotas USING btree (customer_id, project_id);


--
-- Name: ux_projects_customer_project_uuid; Type: INDEX; Schema: cortex; Owner: -
--

CREATE UNIQUE INDEX ux_projects_customer_project_uuid ON cortex.projects USING btree (customer_id, project_id);


--
-- Name: epics_project_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX epics_project_idx ON public.epics USING btree (project, epic_id);


--
-- Name: idx_agent_diaries_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_diaries_agent ON public.agent_diaries USING btree (project, agent_name, created_at DESC);


--
-- Name: idx_agent_diaries_importance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_diaries_importance ON public.agent_diaries USING btree (project, agent_name, importance DESC);


--
-- Name: idx_agent_profiles_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_project ON public.agent_profiles USING btree (project);


--
-- Name: idx_agent_profiles_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_role ON public.agent_profiles USING btree (role);


--
-- Name: idx_agent_profiles_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_profiles_unique ON public.agent_profiles USING btree (project, agent_name, profile_kind, source_file);


--
-- Name: idx_agent_skill_bindings_project_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skill_bindings_project_subject ON public.agent_skill_bindings USING btree (project, subject_kind, lower(subject));


--
-- Name: idx_agent_skill_bindings_skill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skill_bindings_skill ON public.agent_skill_bindings USING btree (project, lower(skill_slug));


--
-- Name: idx_agent_skills_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skills_project ON public.agent_skills USING btree (project, lower(skill_slug));


--
-- Name: idx_agent_skills_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skills_scope ON public.agent_skills USING btree (project, scope);


--
-- Name: idx_agent_skills_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skills_status ON public.agent_skills USING btree (project, status);


--
-- Name: idx_agents_name_project; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agents_name_project ON public.agents USING btree (name, project);


--
-- Name: idx_agents_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_project ON public.agents USING btree (project);


--
-- Name: idx_agents_project_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_project_status ON public.agents USING btree (project, status);


--
-- Name: idx_archive_events_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_events_agent ON public.archive_events USING btree (agent_name);


--
-- Name: idx_archive_events_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_events_ts ON public.archive_events USING btree (ts);


--
-- Name: idx_archive_messages_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_messages_agent ON public.archive_messages USING btree (agent_name);


--
-- Name: idx_archive_messages_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_messages_project ON public.archive_messages USING btree (project);


--
-- Name: idx_archive_messages_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_messages_ts ON public.archive_messages USING btree (ts);


--
-- Name: idx_archive_messages_raw_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_archive_messages_raw_session ON public.archive_messages USING btree (raw_session_id);


--
-- Name: idx_artifact_edges_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifact_edges_project ON public.artifact_edges USING btree (project);


--
-- Name: idx_artifact_edges_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifact_edges_source ON public.artifact_edges USING btree (source_id);


--
-- Name: idx_artifact_edges_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifact_edges_target ON public.artifact_edges USING btree (target_type, target_ref);


--
-- Name: idx_artifacts_caption_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_caption_trgm ON public.artifacts USING gin (lower(COALESCE(caption, ''::text)) public.gin_trgm_ops);

--
-- Name: idx_artifacts_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_embedding_hnsw ON public.artifacts USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_artifacts_modality; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_modality ON public.artifacts USING btree (modality);


--
-- Name: idx_artifacts_neighborhood_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_neighborhood_trgm ON public.artifacts USING gin (lower(COALESCE(neighborhood_text, ''::text)) public.gin_trgm_ops);


--
-- Name: idx_artifacts_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_parent ON public.artifacts USING btree (parent_artifact_id);


--
-- Name: idx_artifacts_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_project ON public.artifacts USING btree (project);


--
-- Name: idx_artifacts_source_file; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_source_file ON public.artifacts USING btree (source_file);


--
-- Name: idx_captured_patterns_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_captured_patterns_active ON public.captured_patterns USING btree (project, is_active) WHERE is_active;


--
-- Name: idx_captured_patterns_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_captured_patterns_project ON public.captured_patterns USING btree (project);


--
-- Name: idx_captured_patterns_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_captured_patterns_search ON public.captured_patterns USING gin (search_vector);


--
-- Name: idx_cortex_actor_aliases_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_actor_aliases_actor ON public.cortex_actor_aliases USING btree (actor_id);


--
-- Name: idx_cortex_actors_project_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_actors_project_status ON public.cortex_actors USING btree (project_id, status);


--
-- Name: idx_cortex_entities_description_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_entities_description_trgm ON public.cortex_entities USING gin (lower(COALESCE((properties ->> 'description'::text), ''::text)) public.gin_trgm_ops);


--
-- Name: idx_cortex_entities_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_entities_name_trgm ON public.cortex_entities USING gin (lower(name) public.gin_trgm_ops);


--
-- Name: idx_cortex_entities_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_entities_project ON public.cortex_entities USING btree (project);


--
-- Name: idx_cortex_entities_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_entities_type ON public.cortex_entities USING btree (entity_type);


--
-- Name: idx_cortex_legacy_identity_archive_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_legacy_identity_archive_project ON public.cortex_legacy_identity_archive USING btree (project_id, project_key);


--
-- Name: idx_cortex_legacy_identity_archive_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_legacy_identity_archive_source ON public.cortex_legacy_identity_archive USING btree (source_schema, source_table, source_pk);


--
-- Name: idx_cortex_project_paths_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_project_paths_project ON public.cortex_project_paths USING btree (project_key);


--
-- Name: idx_cortex_projects_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_projects_parent ON public.cortex_projects USING btree (parent_project_key);


--
-- Name: idx_cortex_projects_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_projects_status ON public.cortex_projects USING btree (status);


--
-- Name: idx_cortex_relationships_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_relationships_project ON public.cortex_relationships USING btree (project);


--
-- Name: idx_cortex_relationships_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_relationships_source ON public.cortex_relationships USING btree (source_entity_id);


--
-- Name: idx_cortex_relationships_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_relationships_target ON public.cortex_relationships USING btree (target_entity_id);


--
-- Name: idx_cortex_relationships_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cortex_relationships_type ON public.cortex_relationships USING btree (relationship_type);


--
-- Name: idx_decisions_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_embedding_hnsw ON public.decisions USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_decisions_files; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_files ON public.decisions USING gin (files_affected);


--
-- Name: idx_decisions_parent_goal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_parent_goal_id ON public.decisions USING btree (parent_goal_id);


--
-- Name: idx_decisions_search_vector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_search_vector ON public.decisions USING gin (search_vector);


--
-- Name: idx_decisions_summary_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_summary_trgm ON public.decisions USING gin (summary public.gin_trgm_ops);


--
-- Name: idx_decisions_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_tags ON public.decisions USING gin (tags);


--
-- Name: idx_decisions_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_project ON public.decisions USING btree (project);


--
-- Name: idx_decisions_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_decisions_agent_id ON public.decisions USING btree (agent_id);


--
-- Name: idx_embedding_backfill_jobs_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_embedding_backfill_jobs_project_created ON public.embedding_backfill_jobs USING btree (project, created_at DESC);


--
-- Name: idx_graph_build_jobs_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_graph_build_jobs_project_created ON public.graph_build_jobs USING btree (project, created_at DESC);


--
-- Name: idx_events_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_agent ON public.team_events USING btree (agent_name);


--
-- Name: idx_events_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_ts ON public.team_events USING btree (ts);


--
-- Name: idx_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_type ON public.team_events USING btree (event_type);


--
-- Name: idx_execution_analyses_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_analyses_agent ON public.execution_analyses USING btree (agent_name);


--
-- Name: idx_execution_analyses_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_analyses_project ON public.execution_analyses USING btree (project);


--
-- Name: idx_execution_analyses_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_analyses_session ON public.execution_analyses USING btree (session_id);


--
-- Name: idx_handoffs_parent_goal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_parent_goal_id ON public.handoffs USING btree (parent_goal_id);


--
-- Name: idx_handoffs_completion_handback_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_handoffs_completion_handback_active ON public.handoffs USING btree (project, reply_to_handoff_id) WHERE ((kind = 'completion_handback'::text) AND (invalidated_at IS NULL) AND (status = ANY (ARRAY['pending'::text, 'claimed'::text])));


--
-- Name: idx_handoffs_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_project ON public.handoffs USING btree (project);


--
-- Name: idx_handoffs_reply_to_handoff_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_reply_to_handoff_id ON public.handoffs USING btree (reply_to_handoff_id);


--
-- Name: idx_handoffs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_status ON public.handoffs USING btree (status);


--
-- Name: idx_handoffs_to_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_to_agent ON public.handoffs USING btree (to_agent);


--
-- Name: idx_handoffs_to_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_to_role ON public.handoffs USING btree (to_role);


--
-- Name: idx_harness_artifacts_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_harness_artifacts_hash ON public.harness_artifacts USING btree (generated_from_hash);


--
-- Name: idx_harness_artifacts_project_harness; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_harness_artifacts_project_harness ON public.harness_artifacts USING btree (project_id, harness);


--
-- Name: idx_harness_artifacts_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_harness_artifacts_status ON public.harness_artifacts USING btree (status);


--
-- Name: idx_knowledge_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_category ON public.knowledge USING btree (category);


--
-- Name: idx_knowledge_content_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_content_trgm ON public.knowledge USING gin (content public.gin_trgm_ops);


--
-- Name: idx_knowledge_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_embedding_hnsw ON public.knowledge USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_knowledge_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project ON public.knowledge USING btree (project);


--
-- Name: idx_knowledge_project_embedding_null; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project_embedding_null ON public.knowledge USING btree (project) WHERE (embedding IS NULL);


--
-- Name: idx_knowledge_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project_id ON public.knowledge USING btree (project_id) WHERE (project_id IS NOT NULL);


--
-- Name: idx_knowledge_room; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_room ON public.knowledge USING btree (((metadata ->> 'room'::text))) WHERE ((metadata ->> 'room'::text) IS NOT NULL);


--
-- Name: idx_knowledge_search_vector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_search_vector ON public.knowledge USING gin (search_vector);


--
-- Name: idx_knowledge_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_source ON public.knowledge USING btree (source_file);


--
-- Name: idx_lessons_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_embedding_hnsw ON public.lessons USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_lessons_search_vector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_search_vector ON public.lessons USING gin (search_vector);


--
-- Name: idx_lessons_summary_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_summary_trgm ON public.lessons USING gin (summary public.gin_trgm_ops);


--
-- Name: idx_lessons_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_project ON public.lessons USING btree (project);


--
-- Name: idx_lessons_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_agent_id ON public.lessons USING btree (agent_id);


--
-- Name: idx_memory_sync_events_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_sync_events_project_created ON public.memory_sync_events USING btree (project_id, created_at DESC);


--
-- Name: idx_memory_sync_events_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_sync_events_result ON public.memory_sync_events USING btree (result);


--
-- Name: idx_memory_sync_events_source_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_sync_events_source_target ON public.memory_sync_events USING btree (source, target);


--
-- Name: idx_messages_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_agent ON public.messages USING btree (agent_name);


--
-- Name: idx_messages_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_embedding_hnsw ON public.messages USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_messages_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_project ON public.messages USING btree (project);


--
-- Name: idx_messages_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_project_ts ON public.messages USING btree (project, ts DESC);


--
-- Name: idx_messages_project_embedding_null; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_project_embedding_null ON public.messages USING btree (project) WHERE (embedding IS NULL);


--
-- Name: idx_messages_search_vector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_search_vector ON public.messages USING gin (search_vector);


--
-- Name: idx_messages_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_session ON public.messages USING btree (session_id);


--
-- Name: idx_messages_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_ts ON public.messages USING btree (ts);


--
-- Name: idx_pattern_metrics_degraded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pattern_metrics_degraded ON public.pattern_metrics USING btree (project, degraded) WHERE degraded;


--
-- Name: idx_profile_bundles_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_profile_bundles_hash ON public.profile_bundles USING btree (content_hash);


--
-- Name: idx_profile_bundles_project_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_profile_bundles_project_agent ON public.profile_bundles USING btree (project_id, lower(agent_name));


--
-- Name: idx_rules_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rules_project ON public.rules USING btree (project, lower(rule_slug));


--
-- Name: idx_rules_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rules_status ON public.rules USING btree (project, status);


--
-- Name: idx_session_sources_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_sources_agent ON public.session_sources USING btree (agent_name);


--
-- Name: idx_session_sources_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_sources_project ON public.session_sources USING btree (project);


--
-- Name: idx_session_sources_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_sources_provider ON public.session_sources USING btree (provider);


--
-- Name: idx_sessions_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_agent ON public.agent_sessions USING btree (agent_id);


--
-- Name: idx_agent_sessions_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sessions_project ON public.agent_sessions USING btree (project);


--
-- Name: idx_sprints_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sprints_project ON public.sprints USING btree (project);


--
-- Name: idx_sprints_project_label; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_sprints_project_label ON public.sprints USING btree (project, sprint_label);


--
-- Name: idx_sprints_project_number; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_sprints_project_number ON public.sprints USING btree (project, sprint_number) WHERE (sprint_number IS NOT NULL);


--
-- Name: idx_tasks_assigned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_assigned ON public.tasks USING btree (assigned_agent);


--
-- Name: idx_tasks_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_project ON public.tasks USING btree (project);


--
-- Name: idx_tasks_sprint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_sprint ON public.tasks USING btree (sprint_id);


--
-- Name: idx_tasks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_status ON public.tasks USING btree (status);


--
-- Name: idx_team_events_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_team_events_project_id ON public.team_events USING btree (project, id);


--
-- Name: idx_team_events_project_ts_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_team_events_project_ts_desc ON public.team_events USING btree (project, ts DESC);


--
-- Name: idx_work_products_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_embedding_hnsw ON public.work_products USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_work_products_file_hashes; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_file_hashes ON public.work_products USING gin (file_hashes);


--
-- Name: idx_work_products_files; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_files ON public.work_products USING gin (files_changed);


--
-- Name: idx_work_products_freshness; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_freshness ON public.work_products USING btree (project, freshness_status, updated_at DESC);


--
-- Name: idx_work_products_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_fts ON public.work_products USING gin (to_tsvector('english'::regconfig, ((((((COALESCE(title, ''::text) || ' '::text) || COALESCE(summary, ''::text)) || ' '::text) || COALESCE(behavior_summary, ''::text)) || ' '::text) || COALESCE(architecture_notes, ''::text))));


--
-- Name: idx_work_products_handoff; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_handoff ON public.work_products USING btree (handoff_id);


--
-- Name: idx_work_products_metadata; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_metadata ON public.work_products USING gin (metadata);


--
-- Name: idx_work_products_project_handoff_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_work_products_project_handoff_current ON public.work_products USING btree (project, handoff_id) WHERE ((handoff_id IS NOT NULL) AND (invalidated_at IS NULL));


--
-- Name: idx_work_products_project_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_project_status ON public.work_products USING btree (project, status, updated_at DESC);


--
-- Name: idx_work_products_projection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_projection ON public.work_products USING btree (project, projection_status, updated_at DESC);


--
-- Name: idx_work_products_subjects; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_subjects ON public.work_products USING gin (subject_entities);


--
-- Name: idx_work_products_summary_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_summary_trgm ON public.work_products USING gin (lower(COALESCE(summary, ''::text)) public.gin_trgm_ops);


--
-- Name: idx_work_products_symbols; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_symbols ON public.work_products USING gin (symbols_changed);


--
-- Name: idx_work_products_title_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_products_title_trgm ON public.work_products USING gin (lower(COALESCE(title, ''::text)) public.gin_trgm_ops);


--
-- Name: uq_amad_loop_passes_inc_pass; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_amad_loop_passes_inc_pass ON public.amad_loop_passes USING btree (increment_id, pass_number);


--
-- Name: ux_cortex_actor_aliases_project_alias; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_cortex_actor_aliases_project_alias ON public.cortex_actor_aliases USING btree (project_id, lower(alias_text));


--
-- Name: ux_cortex_actors_project_slug_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_cortex_actors_project_slug_kind ON public.cortex_actors USING btree (project_id, slug, kind);


--
-- Name: ux_harness_artifacts_project_harness_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_harness_artifacts_project_harness_path ON public.harness_artifacts USING btree (project_id, harness, path);


--
-- Name: ux_profile_bundles_project_agent_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_profile_bundles_project_agent_version ON public.profile_bundles USING btree (project_id, agent_name, version);


--
-- Name: agent_diaries agent_diaries_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER agent_diaries_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.agent_diaries FOR EACH ROW EXECUTE FUNCTION cortex.agent_diaries_update_search_vector();


--
-- Name: artifacts artifacts_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER artifacts_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.artifacts FOR EACH ROW EXECUTE FUNCTION cortex.artifacts_update_search_vector();


--
-- Name: captured_patterns captured_patterns_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER captured_patterns_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.captured_patterns FOR EACH ROW EXECUTE FUNCTION cortex.captured_patterns_update_search_vector();


--
-- Name: decisions decisions_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER decisions_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.decisions FOR EACH ROW EXECUTE FUNCTION cortex.decisions_update_search_vector();


--
-- Name: execution_analyses execution_analyses_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER execution_analyses_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.execution_analyses FOR EACH ROW EXECUTE FUNCTION cortex.execution_analyses_update_search_vector();


--
-- Name: handoffs handoffs_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER handoffs_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.handoffs FOR EACH ROW EXECUTE FUNCTION cortex.handoffs_update_search_vector();


--
-- Name: knowledge knowledge_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER knowledge_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.knowledge FOR EACH ROW EXECUTE FUNCTION cortex.knowledge_update_search_vector();


--
-- Name: lessons lessons_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER lessons_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.lessons FOR EACH ROW EXECUTE FUNCTION cortex.lessons_update_search_vector();


--
-- Name: messages messages_search_vector_tg; Type: TRIGGER; Schema: cortex; Owner: -
--

CREATE TRIGGER messages_search_vector_tg BEFORE INSERT OR UPDATE ON cortex.messages FOR EACH ROW EXECUTE FUNCTION cortex.messages_update_search_vector();


--
-- Name: captured_patterns trg_captured_patterns_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_captured_patterns_search_vector BEFORE INSERT OR UPDATE ON public.captured_patterns FOR EACH ROW EXECUTE FUNCTION public.cortex_tsvector_captured_patterns();


--
-- Name: decisions trg_decisions_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_decisions_search_vector BEFORE INSERT OR UPDATE ON public.decisions FOR EACH ROW EXECUTE FUNCTION public.cortex_tsvector_decisions();


--
-- Name: archive_decisions trg_identity_v2_archive_decisions; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_archive_decisions BEFORE INSERT OR UPDATE OF project, agent_name ON public.archive_decisions FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: archive_events trg_identity_v2_archive_events; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_archive_events BEFORE INSERT OR UPDATE OF project, agent_name ON public.archive_events FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: archive_lessons trg_identity_v2_archive_lessons; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_archive_lessons BEFORE INSERT OR UPDATE OF project, agent_name ON public.archive_lessons FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: archive_messages trg_identity_v2_archive_messages; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_archive_messages BEFORE INSERT OR UPDATE OF project, agent_name ON public.archive_messages FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: decisions trg_identity_v2_decisions; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_decisions BEFORE INSERT OR UPDATE OF project, agent_name ON public.decisions FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: handoffs trg_identity_v2_handoffs; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_handoffs BEFORE INSERT OR UPDATE OF project, from_agent, to_agent, claimed_by ON public.handoffs FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_handoff_row();


--
-- Name: lessons trg_identity_v2_lessons; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_lessons BEFORE INSERT OR UPDATE OF project, agent_name ON public.lessons FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: messages trg_identity_v2_messages; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_messages BEFORE INSERT OR UPDATE OF project, agent_name ON public.messages FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: team_events trg_identity_v2_team_events; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_team_events BEFORE INSERT OR UPDATE OF project, agent_name ON public.team_events FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: work_products trg_identity_v2_work_products; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_identity_v2_work_products BEFORE INSERT OR UPDATE OF project, agent_name ON public.work_products FOR EACH ROW EXECUTE FUNCTION public.cortex_identity_v2_normalize_agent_row();


--
-- Name: knowledge trg_knowledge_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_knowledge_search_vector BEFORE INSERT OR UPDATE ON public.knowledge FOR EACH ROW EXECUTE FUNCTION public.cortex_tsvector_knowledge();


--
-- Name: lessons trg_lessons_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lessons_search_vector BEFORE INSERT OR UPDATE ON public.lessons FOR EACH ROW EXECUTE FUNCTION public.cortex_tsvector_lessons();


--
-- Name: messages trg_messages_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_messages_search_vector BEFORE INSERT OR UPDATE ON public.messages FOR EACH ROW EXECUTE FUNCTION public.cortex_tsvector_messages();


--
-- Name: agent_knowledge_sources agent_knowledge_sources_agent_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.agent_knowledge_sources
    ADD CONSTRAINT agent_knowledge_sources_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES cortex.agent_profiles(id) ON DELETE CASCADE;


--
-- Name: artifact_edges artifact_edges_source_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.artifact_edges
    ADD CONSTRAINT artifact_edges_source_id_fkey FOREIGN KEY (source_id) REFERENCES cortex.artifacts(id) ON DELETE CASCADE;


--
-- Name: cortex_relationships cortex_relationships_source_entity_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_relationships
    ADD CONSTRAINT cortex_relationships_source_entity_id_fkey FOREIGN KEY (source_entity_id) REFERENCES cortex.cortex_entities(id) ON DELETE CASCADE;


--
-- Name: cortex_relationships cortex_relationships_target_entity_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.cortex_relationships
    ADD CONSTRAINT cortex_relationships_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES cortex.cortex_entities(id) ON DELETE CASCADE;


--
-- Name: decisions decisions_parent_decision_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.decisions
    ADD CONSTRAINT decisions_parent_decision_id_fkey FOREIGN KEY (parent_decision_id) REFERENCES cortex.decisions(id) ON DELETE SET NULL;


--
-- Name: handoffs handoffs_supersedes_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.handoffs
    ADD CONSTRAINT handoffs_supersedes_fkey FOREIGN KEY (supersedes) REFERENCES cortex.handoffs(id) ON DELETE SET NULL;


--
-- Name: pattern_metrics pattern_metrics_pattern_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.pattern_metrics
    ADD CONSTRAINT pattern_metrics_pattern_id_fkey FOREIGN KEY (pattern_id) REFERENCES cortex.captured_patterns(id) ON DELETE CASCADE;


--
-- Name: task_executions task_executions_handoff_id_fkey; Type: FK CONSTRAINT; Schema: cortex; Owner: -
--

ALTER TABLE ONLY cortex.task_executions
    ADD CONSTRAINT task_executions_handoff_id_fkey FOREIGN KEY (handoff_id) REFERENCES cortex.handoffs(id) ON DELETE SET NULL;


--
-- Name: agent_sessions agent_sessions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_sessions agent_sessions_handed_off_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_handed_off_to_fkey FOREIGN KEY (handed_off_to) REFERENCES public.agents(id);


--
-- Name: agent_sessions agent_sessions_sprint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_sprint_id_fkey FOREIGN KEY (sprint_id) REFERENCES public.sprints(id);


--
-- Name: artifact_edges artifact_edges_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_edges
    ADD CONSTRAINT artifact_edges_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.artifacts(id) ON DELETE CASCADE;


--
-- Name: artifacts artifacts_parent_artifact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_parent_artifact_id_fkey FOREIGN KEY (parent_artifact_id) REFERENCES public.artifacts(id) ON DELETE SET NULL;


--
-- Name: captured_patterns captured_patterns_parent_pattern_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.captured_patterns
    ADD CONSTRAINT captured_patterns_parent_pattern_id_fkey FOREIGN KEY (parent_pattern_id) REFERENCES public.captured_patterns(id);


--
-- Name: cortex_actor_aliases cortex_actor_aliases_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_actor_aliases
    ADD CONSTRAINT cortex_actor_aliases_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id) ON DELETE CASCADE;


--
-- Name: cortex_actor_aliases cortex_actor_aliases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_actor_aliases
    ADD CONSTRAINT cortex_actor_aliases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id) ON DELETE CASCADE;


--
-- Name: cortex_actors cortex_actors_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_actors
    ADD CONSTRAINT cortex_actors_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id) ON DELETE CASCADE;


--
-- Name: cortex_project_paths cortex_project_paths_project_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_project_paths
    ADD CONSTRAINT cortex_project_paths_project_key_fkey FOREIGN KEY (project_key) REFERENCES public.cortex_projects(project_key) ON DELETE CASCADE;


--
-- Name: cortex_projects cortex_projects_parent_project_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_projects
    ADD CONSTRAINT cortex_projects_parent_project_key_fkey FOREIGN KEY (parent_project_key) REFERENCES public.cortex_projects(project_key);


--
-- Name: cortex_relationships cortex_relationships_source_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_relationships
    ADD CONSTRAINT cortex_relationships_source_entity_id_fkey FOREIGN KEY (source_entity_id) REFERENCES public.cortex_entities(id);


--
-- Name: cortex_relationships cortex_relationships_target_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_relationships
    ADD CONSTRAINT cortex_relationships_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES public.cortex_entities(id);


--
-- Name: decisions decisions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT decisions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: decisions decisions_parent_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT decisions_parent_decision_id_fkey FOREIGN KEY (parent_decision_id) REFERENCES public.decisions(id);


--
-- Name: decisions decisions_sprint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT decisions_sprint_id_fkey FOREIGN KEY (sprint_id) REFERENCES public.sprints(id);


--
-- Name: decisions decisions_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT decisions_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.decisions(id);


--
-- Name: agent_diaries fk_identity_v2_agent_diaries_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_diaries
    ADD CONSTRAINT fk_identity_v2_agent_diaries_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: agent_diaries fk_identity_v2_agent_diaries_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_diaries
    ADD CONSTRAINT fk_identity_v2_agent_diaries_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: agent_profiles fk_identity_v2_agent_profiles_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT fk_identity_v2_agent_profiles_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: agent_profiles fk_identity_v2_agent_profiles_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT fk_identity_v2_agent_profiles_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: agent_sessions fk_identity_v2_agent_sessions_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT fk_identity_v2_agent_sessions_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: agents fk_identity_v2_agents_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_identity_v2_agents_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: agents fk_identity_v2_agents_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_identity_v2_agents_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: archive_decisions fk_identity_v2_archive_decisions_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_decisions
    ADD CONSTRAINT fk_identity_v2_archive_decisions_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: archive_decisions fk_identity_v2_archive_decisions_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_decisions
    ADD CONSTRAINT fk_identity_v2_archive_decisions_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: archive_events fk_identity_v2_archive_events_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_events
    ADD CONSTRAINT fk_identity_v2_archive_events_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: archive_events fk_identity_v2_archive_events_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_events
    ADD CONSTRAINT fk_identity_v2_archive_events_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: archive_handoffs fk_identity_v2_archive_handoffs_from_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_handoffs
    ADD CONSTRAINT fk_identity_v2_archive_handoffs_from_actor_id FOREIGN KEY (from_actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: archive_handoffs fk_identity_v2_archive_handoffs_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_handoffs
    ADD CONSTRAINT fk_identity_v2_archive_handoffs_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: archive_lessons fk_identity_v2_archive_lessons_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_lessons
    ADD CONSTRAINT fk_identity_v2_archive_lessons_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: archive_lessons fk_identity_v2_archive_lessons_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_lessons
    ADD CONSTRAINT fk_identity_v2_archive_lessons_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: archive_messages fk_identity_v2_archive_messages_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_messages
    ADD CONSTRAINT fk_identity_v2_archive_messages_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: archive_messages fk_identity_v2_archive_messages_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.archive_messages
    ADD CONSTRAINT fk_identity_v2_archive_messages_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: artifact_edges fk_identity_v2_artifact_edges_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifact_edges
    ADD CONSTRAINT fk_identity_v2_artifact_edges_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: artifacts fk_identity_v2_artifacts_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT fk_identity_v2_artifacts_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: cortex_entities fk_identity_v2_cortex_entities_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_entities
    ADD CONSTRAINT fk_identity_v2_cortex_entities_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: cortex_relationships fk_identity_v2_cortex_relationships_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cortex_relationships
    ADD CONSTRAINT fk_identity_v2_cortex_relationships_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: decisions fk_identity_v2_decisions_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT fk_identity_v2_decisions_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: decisions fk_identity_v2_decisions_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decisions
    ADD CONSTRAINT fk_identity_v2_decisions_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: handoffs fk_identity_v2_handoffs_claimed_by_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT fk_identity_v2_handoffs_claimed_by_actor_id FOREIGN KEY (claimed_by_actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: handoffs fk_identity_v2_handoffs_from_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT fk_identity_v2_handoffs_from_actor_id FOREIGN KEY (from_actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: handoffs fk_identity_v2_handoffs_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT fk_identity_v2_handoffs_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: handoffs fk_identity_v2_handoffs_to_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT fk_identity_v2_handoffs_to_actor_id FOREIGN KEY (to_actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: handoffs handoffs_reply_to_handoff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT handoffs_reply_to_handoff_id_fkey FOREIGN KEY (reply_to_handoff_id) REFERENCES public.handoffs(id);


--
-- Name: knowledge fk_identity_v2_knowledge_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge
    ADD CONSTRAINT fk_identity_v2_knowledge_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: lessons fk_identity_v2_lessons_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT fk_identity_v2_lessons_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: lessons fk_identity_v2_lessons_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT fk_identity_v2_lessons_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: messages fk_identity_v2_messages_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT fk_identity_v2_messages_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: messages fk_identity_v2_messages_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT fk_identity_v2_messages_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: tasks fk_identity_v2_tasks_assigned_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_identity_v2_tasks_assigned_actor_id FOREIGN KEY (assigned_actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: tasks fk_identity_v2_tasks_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_identity_v2_tasks_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: team_events fk_identity_v2_team_events_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events
    ADD CONSTRAINT fk_identity_v2_team_events_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: team_events fk_identity_v2_team_events_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events
    ADD CONSTRAINT fk_identity_v2_team_events_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: work_products fk_identity_v2_work_products_actor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_products
    ADD CONSTRAINT fk_identity_v2_work_products_actor_id FOREIGN KEY (actor_id) REFERENCES public.cortex_actors(id);


--
-- Name: work_products fk_identity_v2_work_products_project_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_products
    ADD CONSTRAINT fk_identity_v2_work_products_project_id FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id);


--
-- Name: handoffs handoffs_sprint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoffs
    ADD CONSTRAINT handoffs_sprint_id_fkey FOREIGN KEY (sprint_id) REFERENCES public.sprints(id);


--
-- Name: harness_artifacts harness_artifacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.harness_artifacts
    ADD CONSTRAINT harness_artifacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id) ON DELETE CASCADE;


--
-- Name: lessons lessons_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: lessons lessons_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES public.decisions(id);


--
-- Name: memory_sync_events memory_sync_events_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_sync_events
    ADD CONSTRAINT memory_sync_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id) ON DELETE CASCADE;


--
-- Name: messages messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.agent_sessions(id);


--
-- Name: profile_bundles profile_bundles_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profile_bundles
    ADD CONSTRAINT profile_bundles_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.cortex_projects(id) ON DELETE CASCADE;


--
-- Name: session_sources session_sources_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_sources
    ADD CONSTRAINT session_sources_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.agent_sessions(id) ON DELETE CASCADE;


--
-- Name: tasks tasks_blocked_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_blocked_by_fkey FOREIGN KEY (blocked_by) REFERENCES public.tasks(id);


--
-- Name: tasks tasks_sprint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_sprint_id_fkey FOREIGN KEY (sprint_id) REFERENCES public.sprints(id);


--
-- Name: team_events team_events_related_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events
    ADD CONSTRAINT team_events_related_decision_id_fkey FOREIGN KEY (related_decision_id) REFERENCES public.decisions(id);


--
-- Name: team_events team_events_sprint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_events
    ADD CONSTRAINT team_events_sprint_id_fkey FOREIGN KEY (sprint_id) REFERENCES public.sprints(id);


--
-- Name: agent_diaries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_diaries ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_diaries agent_diaries_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_diaries_project_isolation ON public.agent_diaries USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: agent_profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_profiles agent_profiles_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_profiles_project_isolation ON public.agent_profiles USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: agent_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_sessions agent_sessions_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_sessions_project_isolation ON public.agent_sessions USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;

--
-- Name: agents agents_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agents_project_isolation ON public.agents USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: archive_decisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.archive_decisions ENABLE ROW LEVEL SECURITY;

--
-- Name: archive_decisions archive_decisions_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY archive_decisions_project_isolation ON public.archive_decisions USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: archive_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.archive_events ENABLE ROW LEVEL SECURITY;

--
-- Name: archive_events archive_events_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY archive_events_project_isolation ON public.archive_events USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: archive_handoffs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.archive_handoffs ENABLE ROW LEVEL SECURITY;

--
-- Name: archive_handoffs archive_handoffs_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY archive_handoffs_project_isolation ON public.archive_handoffs USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: archive_lessons; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.archive_lessons ENABLE ROW LEVEL SECURITY;

--
-- Name: archive_lessons archive_lessons_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY archive_lessons_project_isolation ON public.archive_lessons USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: archive_messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.archive_messages ENABLE ROW LEVEL SECURITY;

--
-- Name: archive_messages archive_messages_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY archive_messages_project_isolation ON public.archive_messages USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: artifact_edges; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.artifact_edges ENABLE ROW LEVEL SECURITY;

--
-- Name: artifact_edges artifact_edges_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY artifact_edges_project_isolation ON public.artifact_edges USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: artifacts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.artifacts ENABLE ROW LEVEL SECURITY;

--
-- Name: artifacts artifacts_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY artifacts_project_isolation ON public.artifacts USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: captured_patterns; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.captured_patterns ENABLE ROW LEVEL SECURITY;

--
-- Name: captured_patterns captured_patterns_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY captured_patterns_project_isolation ON public.captured_patterns USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: cortex_audit_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cortex_audit_log ENABLE ROW LEVEL SECURITY;

--
-- Name: cortex_audit_log cortex_audit_log_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cortex_audit_log_project_isolation ON public.cortex_audit_log USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: cortex_entities; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cortex_entities ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.cortex_entities FORCE ROW LEVEL SECURITY;

--
-- Name: cortex_entities cortex_entities_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cortex_entities_project_isolation ON public.cortex_entities USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: cortex_project_paths; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cortex_project_paths ENABLE ROW LEVEL SECURITY;

--
-- Name: cortex_project_paths cortex_project_paths_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cortex_project_paths_project_isolation ON public.cortex_project_paths USING ((project_key = current_setting('cortex.project'::text, true))) WITH CHECK ((project_key = current_setting('cortex.project'::text, true)));


--
-- Name: cortex_projects; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cortex_projects ENABLE ROW LEVEL SECURITY;

--
-- Name: cortex_projects cortex_projects_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cortex_projects_project_isolation ON public.cortex_projects USING ((project_key = current_setting('cortex.project'::text, true))) WITH CHECK ((project_key = current_setting('cortex.project'::text, true)));


--
-- Name: cortex_relationships; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cortex_relationships ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.cortex_relationships FORCE ROW LEVEL SECURITY;

--
-- Name: cortex_relationships cortex_relationships_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cortex_relationships_project_isolation ON public.cortex_relationships USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: decisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.decisions ENABLE ROW LEVEL SECURITY;

--
-- Name: decisions decisions_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY decisions_project_isolation ON public.decisions USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: epics; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.epics ENABLE ROW LEVEL SECURITY;

--
-- Name: epics epics_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY epics_project_isolation ON public.epics USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: execution_analyses; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.execution_analyses ENABLE ROW LEVEL SECURITY;

--
-- Name: execution_analyses execution_analyses_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY execution_analyses_project_isolation ON public.execution_analyses USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: handoffs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.handoffs ENABLE ROW LEVEL SECURITY;

--
-- Name: handoffs handoffs_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY handoffs_project_isolation ON public.handoffs USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: harness_artifacts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.harness_artifacts ENABLE ROW LEVEL SECURITY;

--
-- Name: harness_artifacts harness_artifacts_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY harness_artifacts_project_isolation ON public.harness_artifacts USING ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true))))) WITH CHECK ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true)))));


--
-- Name: knowledge; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.knowledge ENABLE ROW LEVEL SECURITY;

--
-- Name: knowledge knowledge_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY knowledge_project_isolation ON public.knowledge USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: lessons; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lessons ENABLE ROW LEVEL SECURITY;

--
-- Name: lessons lessons_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY lessons_project_isolation ON public.lessons USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: memory_sync_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_sync_events ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_sync_events memory_sync_events_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY memory_sync_events_project_isolation ON public.memory_sync_events USING ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true))))) WITH CHECK ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true)))));


--
-- Name: messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

--
-- Name: messages messages_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY messages_project_isolation ON public.messages USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: pattern_metrics; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pattern_metrics ENABLE ROW LEVEL SECURITY;

--
-- Name: pattern_metrics pattern_metrics_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pattern_metrics_project_isolation ON public.pattern_metrics USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: profile_bundles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.profile_bundles ENABLE ROW LEVEL SECURITY;

--
-- Name: profile_bundles profile_bundles_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profile_bundles_project_isolation ON public.profile_bundles USING ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true))))) WITH CHECK ((project_id = ( SELECT cortex_projects.id
   FROM public.cortex_projects
  WHERE (cortex_projects.project_key = current_setting('cortex.project'::text, true)))));


--
-- Name: roles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.roles ENABLE ROW LEVEL SECURITY;

--
-- Name: roles roles_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY roles_project_isolation ON public.roles USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: session_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session_sources ENABLE ROW LEVEL SECURITY;

--
-- Name: session_sources session_sources_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY session_sources_project_isolation ON public.session_sources USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: sprints; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sprints ENABLE ROW LEVEL SECURITY;

--
-- Name: sprints sprints_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY sprints_project_isolation ON public.sprints USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: tasks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;

--
-- Name: tasks tasks_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tasks_project_isolation ON public.tasks USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: team_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.team_events ENABLE ROW LEVEL SECURITY;

--
-- Name: team_events team_events_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY team_events_project_isolation ON public.team_events USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- Name: work_products; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.work_products ENABLE ROW LEVEL SECURITY;

--
-- Name: work_products work_products_project_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY work_products_project_isolation ON public.work_products USING (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text))) WITH CHECK (((project = current_setting('cortex.project'::text, true)) OR (project = '_global'::text)));


--
-- PostgreSQL database dump complete
--

\unrestrict b21kmeFBmTyjubhrzziLCMw7SQ1xFcNHoLxHkZbDodcNON273CfHhhpz7w65hAd
