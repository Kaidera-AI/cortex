DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'processing migration must run as cortex_v2_migrator';
    END IF;
    -- pgvector 0.8.6 is not a trusted extension, so the deliberately
    -- non-superuser migrator role cannot install it. The database owner
    -- (cortex_v2_owner) installs it once into public; fail closed with an
    -- actionable message instead of half-creating the projection plane.
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_type AS t
          JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
         WHERE t.typname = 'vector'
           AND n.nspname = 'public'
    ) THEN
        RAISE EXCEPTION
            'pgvector is not installed in public: the database owner must run '
            'CREATE EXTENSION vector SCHEMA public before 0004_processing.sql';
    END IF;
END
$bootstrap$;

CREATE SCHEMA IF NOT EXISTS cortex_processing AUTHORIZATION cortex_v2_migrator;

-- ---------------------------------------------------------------------------
-- Immutable processing profiles: extractor, chunker, embedder selection,
-- optional versioned transforms and the intended-coverage selection that R13
-- measures against. A profile is never edited; a change is a new version row.
-- installation_id IS NULL marks a built-in profile that every installation may
-- use. That is a registry default, not tenant data and not a NULL scope: no
-- row in this schema ever treats a NULL scope as global ownership.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.processing_profiles (
    profile_id uuid PRIMARY KEY,
    installation_id uuid REFERENCES cortex_auth.installations(installation_id),
    profile_key text NOT NULL CHECK (length(profile_key) BETWEEN 1 AND 96),
    profile_version integer NOT NULL CHECK (profile_version > 0),
    parser jsonb NOT NULL,
    chunking jsonb NOT NULL,
    embedder jsonb NOT NULL,
    transforms jsonb NOT NULL DEFAULT '[]'::jsonb,
    coverage jsonb NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_by_principal uuid REFERENCES cortex_auth.principals(principal_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (installation_id, profile_key, profile_version)
);

CREATE FUNCTION cortex_processing.profile_shape_violation(
    p_parser jsonb,
    p_chunking jsonb,
    p_embedder jsonb,
    p_transforms jsonb,
    p_coverage jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    transform jsonb;
    transform_kind text;
BEGIN
    IF jsonb_typeof(p_parser) <> 'object' THEN
        RETURN 'parser must be a JSON object';
    END IF;
    IF (p_parser ->> 'schema_version')::text IS DISTINCT FROM '1' THEN
        RETURN 'parser.schema_version must be 1';
    END IF;
    IF p_parser ->> 'kind' NOT IN ('auto', 'text', 'markdown', 'delimited', 'json',
                                   'source_code', 'mail', 'xml', 'yaml', 'rtf',
                                   'pdf', 'ooxml', 'opendocument', 'epub') THEN
        RETURN 'parser.kind is not a known extractor kind';
    END IF;
    IF p_parser ->> 'executor_role' NOT IN ('core', 'doc', 'image', 'audio', 'video') THEN
        RETURN 'parser.executor_role is not a known executor role';
    END IF;
    IF (p_parser ->> 'version') !~ '^[0-9]{1,6}$' THEN
        RETURN 'parser.version must be a positive integer';
    END IF;
    IF jsonb_typeof(p_parser -> 'formats') NOT IN ('array', 'null') THEN
        RETURN 'parser.formats must be an array of format ids or "*"';
    END IF;

    IF jsonb_typeof(p_chunking) <> 'object' THEN
        RETURN 'chunking must be a JSON object';
    END IF;
    IF (p_chunking ->> 'schema_version')::text IS DISTINCT FROM '1' THEN
        RETURN 'chunking.schema_version must be 1';
    END IF;
    IF length(COALESCE(p_chunking ->> 'policy_id', '')) NOT BETWEEN 1 AND 64 THEN
        RETURN 'chunking.policy_id is required';
    END IF;
    IF (p_chunking ->> 'version') !~ '^[0-9]{1,6}$' THEN
        RETURN 'chunking.version must be a positive integer';
    END IF;
    IF p_chunking ->> 'kind' NOT IN ('structural', 'token_window') THEN
        RETURN 'chunking.kind must be structural or token_window';
    END IF;
    IF (p_chunking ->> 'target_chars') !~ '^[0-9]{1,7}$'
       OR (p_chunking ->> 'max_chars') !~ '^[0-9]{1,7}$'
       OR (p_chunking ->> 'overlap_chars') !~ '^[0-9]{1,7}$' THEN
        RETURN 'chunking character bounds must be non-negative integers';
    END IF;
    IF (p_chunking ->> 'max_chars')::integer < (p_chunking ->> 'target_chars')::integer THEN
        RETURN 'chunking.max_chars must be at least chunking.target_chars';
    END IF;
    IF (p_chunking ->> 'overlap_chars')::integer >= (p_chunking ->> 'target_chars')::integer THEN
        RETURN 'chunking.overlap_chars must be smaller than chunking.target_chars';
    END IF;

    IF jsonb_typeof(p_embedder) <> 'object' THEN
        RETURN 'embedder must be a JSON object';
    END IF;
    IF (p_embedder ->> 'schema_version')::text IS DISTINCT FROM '1' THEN
        RETURN 'embedder.schema_version must be 1';
    END IF;
    IF p_embedder ->> 'role' <> 'embedding' THEN
        RETURN 'embedder.role must be embedding';
    END IF;
    IF p_embedder ->> 'space_selection' NOT IN ('explicit', 'active') THEN
        RETURN 'embedder.space_selection must be explicit or active';
    END IF;

    IF jsonb_typeof(p_transforms) <> 'array' THEN
        RETURN 'transforms must be a JSON array';
    END IF;
    FOR transform IN SELECT * FROM jsonb_array_elements(p_transforms)
    LOOP
        IF jsonb_typeof(transform) <> 'object' THEN
            RETURN 'each transform must be a JSON object';
        END IF;
        transform_kind := transform ->> 'kind';
        IF transform_kind NOT IN ('distill', 'compact') THEN
            RETURN 'transform.kind must be distill or compact';
        END IF;
        IF length(COALESCE(transform ->> 'name', '')) NOT BETWEEN 1 AND 64 THEN
            RETURN 'transform.name is required';
        END IF;
        IF (transform ->> 'version') !~ '^[0-9]{1,6}$' THEN
            RETURN 'transform.version must be a positive integer';
        END IF;
        IF jsonb_typeof(transform -> 'enabled') <> 'boolean' THEN
            RETURN 'transform.enabled must be a boolean';
        END IF;
        IF jsonb_typeof(transform -> 'params') NOT IN ('object', 'null') THEN
            RETURN 'transform.params must be a JSON object';
        END IF;
    END LOOP;

    IF jsonb_typeof(p_coverage) <> 'object' THEN
        RETURN 'coverage must be a JSON object';
    END IF;
    IF (p_coverage ->> 'schema_version')::text IS DISTINCT FROM '1' THEN
        RETURN 'coverage.schema_version must be 1';
    END IF;
    IF jsonb_typeof(p_coverage -> 'content_classes') <> 'array'
       OR jsonb_array_length(p_coverage -> 'content_classes') = 0 THEN
        RETURN 'coverage.content_classes must be a non-empty array';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements_text(p_coverage -> 'content_classes') AS klass
         WHERE klass NOT IN ('decision', 'lesson', 'knowledge', 'progress', 'diary',
                             'message', 'session', 'artifact', 'work_product')
    ) THEN
        RETURN 'coverage.content_classes contains an unknown content class';
    END IF;
    IF (p_coverage ->> 'min_text_length') !~ '^[0-9]{1,7}$' THEN
        RETURN 'coverage.min_text_length must be a non-negative integer';
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION cortex_processing.validate_processing_profile()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    violation text;
BEGIN
    violation := cortex_processing.profile_shape_violation(
        NEW.parser, NEW.chunking, NEW.embedder, NEW.transforms, NEW.coverage
    );
    IF violation IS NOT NULL THEN
        RAISE EXCEPTION '%', violation USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.retire_processing_profile()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.status = OLD.status THEN
        RAISE EXCEPTION 'processing profiles are immutable; create a new version'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'active' OR NEW.status <> 'retired' THEN
        RAISE EXCEPTION 'the only profile transition is active -> retired'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.reject_processing_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'projection and ledger rows are append-only' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER processing_profiles_validate_shape
    BEFORE INSERT ON cortex_processing.processing_profiles
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_processing_profile();

CREATE TRIGGER processing_profiles_retire_only
    BEFORE UPDATE ON cortex_processing.processing_profiles
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.retire_processing_profile();

CREATE TRIGGER processing_profiles_reject_delete
    BEFORE DELETE ON cortex_processing.processing_profiles
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

-- Built-in profiles. Deterministic ids so an operator, a test and a rebuild all
-- address the same profile. Transforms are disabled by default (R18) and the
-- doc profile is the only one that routes work to the optional doc executor.
INSERT INTO cortex_processing.processing_profiles (
    profile_id, installation_id, profile_key, profile_version,
    parser, chunking, embedder, transforms, coverage, status
) VALUES
(
    'c2a00000-0000-4000-8000-000000000001', NULL, 'core.text', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["text", "unknown_text"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.paragraph", "version": 1, "kind": "structural", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 0}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[]'::jsonb,
    '{"schema_version": 1, "content_classes": ["decision", "lesson", "knowledge", "progress", "diary", "message", "work_product"], "min_text_length": 24}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000002', NULL, 'core.markdown', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["markdown", "html"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.paragraph", "version": 1, "kind": "structural", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 0}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[]'::jsonb,
    '{"schema_version": 1, "content_classes": ["knowledge", "decision", "lesson", "work_product", "artifact"], "min_text_length": 24}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000003', NULL, 'core.json', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["json", "jsonl"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.token-window", "version": 1, "kind": "token_window", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 120}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[]'::jsonb,
    '{"schema_version": 1, "content_classes": ["knowledge", "artifact", "session"], "min_text_length": 16}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000004', NULL, 'core.source-code', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["source_code"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.token-window", "version": 1, "kind": "token_window", "target_chars": 1600, "max_chars": 2400, "overlap_chars": 160}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[]'::jsonb,
    '{"schema_version": 1, "content_classes": ["artifact", "work_product", "knowledge"], "min_text_length": 16}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000005', NULL, 'doc.auto', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "doc", "formats": ["*"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.paragraph", "version": 1, "kind": "structural", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 0}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[]'::jsonb,
    '{"schema_version": 1, "content_classes": ["artifact", "knowledge", "work_product", "session"], "min_text_length": 1}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000006', NULL, 'optional.distill', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["*"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.paragraph", "version": 1, "kind": "structural", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 0}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[{"kind": "distill", "name": "distill.extractive", "version": 1, "enabled": false, "params": {"ratio": 0.3}}]'::jsonb,
    '{"schema_version": 1, "content_classes": ["decision", "lesson", "knowledge", "session"], "min_text_length": 240}'::jsonb,
    'active'
),
(
    'c2a00000-0000-4000-8000-000000000007', NULL, 'optional.compact', 1,
    '{"schema_version": 1, "kind": "auto", "version": 1, "executor_role": "core", "formats": ["*"]}'::jsonb,
    '{"schema_version": 1, "policy_id": "chunk.paragraph", "version": 1, "kind": "structural", "target_chars": 1200, "max_chars": 2000, "overlap_chars": 0}'::jsonb,
    '{"schema_version": 1, "role": "embedding", "space_selection": "explicit"}'::jsonb,
    '[{"kind": "compact", "name": "compact.dedupe", "version": 1, "enabled": false, "params": {}}]'::jsonb,
    '{"schema_version": 1, "content_classes": ["message", "session", "progress"], "min_text_length": 240}'::jsonb,
    'active'
);

-- ---------------------------------------------------------------------------
-- Embedding spaces and index generations. A space is immutable model semantics
-- (F07): provider, served model identity, dimensions, normalization, metric,
-- prefixes and the chunking policy its chunks were built with. The only mutable
-- column is the active-generation pointer, and it may only name a generation of
-- the same space that is in state 'active'.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.embedding_spaces (
    space_id uuid PRIMARY KEY,
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    space_name text NOT NULL CHECK (length(space_name) BETWEEN 1 AND 96),
    provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    model_revision text CHECK (model_revision IS NULL OR length(model_revision) BETWEEN 1 AND 128),
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    normalization text NOT NULL CHECK (normalization IN ('l2', 'none')),
    metric text NOT NULL CHECK (metric IN ('cosine', 'l2', 'inner_product')),
    query_prefix text CHECK (query_prefix IS NULL OR length(query_prefix) BETWEEN 1 AND 256),
    document_prefix text CHECK (document_prefix IS NULL OR length(document_prefix) BETWEEN 1 AND 256),
    chunking jsonb NOT NULL,
    active_generation_id uuid,
    created_by_principal uuid REFERENCES cortex_auth.principals(principal_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (installation_id, space_name)
);

CREATE TABLE cortex_processing.index_generations (
    generation_id uuid PRIMARY KEY,
    space_id uuid NOT NULL REFERENCES cortex_processing.embedding_spaces(space_id),
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    generation integer NOT NULL CHECK (generation > 0),
    state text NOT NULL DEFAULT 'building'
        CHECK (state IN ('building', 'built', 'active', 'retired', 'failed')),
    chunking jsonb NOT NULL,
    profile_id uuid REFERENCES cortex_processing.processing_profiles(profile_id),
    created_by_principal uuid REFERENCES cortex_auth.principals(principal_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    built_at timestamptz,
    activated_at timestamptz,
    retired_at timestamptz,
    failure_code text CHECK (failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64),
    UNIQUE (space_id, generation)
);

ALTER TABLE cortex_processing.embedding_spaces
    ADD CONSTRAINT embedding_spaces_active_generation_fk
    FOREIGN KEY (active_generation_id)
    REFERENCES cortex_processing.index_generations(generation_id)
    DEFERRABLE INITIALLY DEFERRED;

-- Exactly one active generation per space, and the pointer must agree with it.
CREATE UNIQUE INDEX index_generations_one_active
    ON cortex_processing.index_generations(space_id)
    WHERE state = 'active';

CREATE FUNCTION cortex_processing.validate_generation_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state NOT IN ('building', 'active') THEN
            RAISE EXCEPTION 'a new generation starts building' USING ERRCODE = '23514';
        END IF;
        IF NEW.generation > 1 AND NEW.state = 'active' THEN
            RAISE EXCEPTION 'only generation 1 may be created active' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.generation_id <> OLD.generation_id
       OR NEW.space_id <> OLD.space_id
       OR NEW.generation <> OLD.generation
       OR NEW.installation_id <> OLD.installation_id
       OR NEW.chunking IS DISTINCT FROM OLD.chunking THEN
        RAISE EXCEPTION 'generation identity and chunking snapshot are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.state = 'building' AND NEW.state IN ('built', 'failed', 'active'))
        OR (OLD.state = 'built' AND NEW.state IN ('active', 'failed'))
        OR (OLD.state = 'active' AND NEW.state = 'retired')
        OR (OLD.state = 'built' AND NEW.state = 'retired')
        OR OLD.state = NEW.state
    ) THEN
        RAISE EXCEPTION 'generation transition % -> % is not allowed', OLD.state, NEW.state
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state = 'built' AND NEW.built_at IS NULL THEN
        NEW.built_at := pg_catalog.now();
    END IF;
    IF NEW.state = 'active' AND NEW.activated_at IS NULL THEN
        NEW.activated_at := pg_catalog.now();
    END IF;
    IF NEW.state = 'retired' AND NEW.retired_at IS NULL THEN
        NEW.retired_at := pg_catalog.now();
    END IF;
    IF NEW.state = 'failed' AND NEW.failure_code IS NULL THEN
        RAISE EXCEPTION 'a failed generation records its failure code' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.validate_space_pointer()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.space_id <> OLD.space_id
       OR NEW.installation_id <> OLD.installation_id
       OR NEW.space_name <> OLD.space_name
       OR NEW.provider <> OLD.provider
       OR NEW.model_id <> OLD.model_id
       OR NEW.model_revision IS DISTINCT FROM OLD.model_revision
       OR NEW.dimensions <> OLD.dimensions
       OR NEW.normalization <> OLD.normalization
       OR NEW.metric <> OLD.metric
       OR NEW.query_prefix IS DISTINCT FROM OLD.query_prefix
       OR NEW.document_prefix IS DISTINCT FROM OLD.document_prefix
       OR NEW.chunking IS DISTINCT FROM OLD.chunking THEN
        RAISE EXCEPTION
            'embedding space semantics are immutable; a model or chunking change is a new space'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.validate_space_generation_consistency()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    target_space uuid;
    pointer uuid;
    active_count integer;
BEGIN
    target_space := NEW.space_id;
    SELECT active_generation_id
      INTO pointer
      FROM cortex_processing.embedding_spaces
     WHERE space_id = target_space;
    SELECT count(*)
      INTO active_count
      FROM cortex_processing.index_generations
     WHERE space_id = target_space
       AND state = 'active';

    IF pointer IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1
              FROM cortex_processing.index_generations
             WHERE generation_id = pointer
               AND space_id = target_space
               AND state = 'active'
        ) THEN
            RAISE EXCEPTION 'the active generation pointer must reference an active generation of the same space'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    IF active_count > 1 THEN
        RAISE EXCEPTION 'a space has more than one active generation' USING ERRCODE = '55000';
    END IF;
    IF active_count = 1 AND pointer IS NULL THEN
        RAISE EXCEPTION 'an active generation must be named by the space pointer'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END
$function$;

CREATE TRIGGER index_generations_validate_transition
    BEFORE UPDATE ON cortex_processing.index_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_generation_transition();

CREATE TRIGGER index_generations_validate_insert
    BEFORE INSERT ON cortex_processing.index_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_generation_transition();

CREATE TRIGGER index_generations_reject_delete
    BEFORE DELETE ON cortex_processing.index_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE CONSTRAINT TRIGGER index_generations_pointer_consistency
    AFTER INSERT OR UPDATE OF state ON cortex_processing.index_generations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_space_generation_consistency();

CREATE TRIGGER embedding_spaces_validate_update
    BEFORE UPDATE ON cortex_processing.embedding_spaces
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_space_pointer();

CREATE TRIGGER embedding_spaces_reject_delete
    BEFORE DELETE ON cortex_processing.embedding_spaces
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE CONSTRAINT TRIGGER embedding_spaces_pointer_consistency
    AFTER UPDATE OF active_generation_id ON cortex_processing.embedding_spaces
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_space_generation_consistency();

CREATE INDEX index_generations_space_state
    ON cortex_processing.index_generations(space_id, state, generation DESC);

-- ---------------------------------------------------------------------------
-- Durable work: jobs, attempts, leases with fencing epochs, budget reservations
-- and the quarantine ledger. No accepted required work exists only in a process
-- task (F06); an expired lease can never publish because publication re-checks
-- epoch, owner, expiry and cancellation in the same transaction.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.jobs (
    job_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    job_kind text NOT NULL CHECK (job_kind IN (
        'doc.extract', 'embed.chunks', 'transform.distill', 'transform.compact',
        'graph.code.extract', 'graph.memory.extract'
    )),
    dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 8 AND 128),
    required_role text NOT NULL DEFAULT 'core'
        CHECK (required_role IN ('core', 'doc', 'graph', 'image', 'audio', 'video')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'leased', 'succeeded', 'failed', 'cancelled', 'quarantined', 'blocked'
    )),
    priority integer NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 1000),
    available_at timestamptz NOT NULL DEFAULT now(),
    content_id uuid,
    source_revision integer CHECK (source_revision > 0),
    profile_id uuid REFERENCES cortex_processing.processing_profiles(profile_id),
    space_id uuid REFERENCES cortex_processing.embedding_spaces(space_id),
    generation_id uuid REFERENCES cortex_processing.index_generations(generation_id),
    batch_id uuid,
    intent jsonb NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 100),
    fencing_epoch integer NOT NULL DEFAULT 0 CHECK (fencing_epoch >= 0),
    lease_owner text CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 128),
    lease_expires_at timestamptz,
    rerouted_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    cancelled_at timestamptz,
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    error_message text CHECK (error_message IS NULL OR length(error_message) <= 512),
    error_retryable boolean,
    requested_by_principal uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (scope_id, job_kind, dedupe_key),
    CHECK ((content_id IS NULL) = (source_revision IS NULL)),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    FOREIGN KEY (scope_id, content_id, source_revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT,
    FOREIGN KEY (requested_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_processing.job_intent_violation(
    p_job_kind text,
    p_intent jsonb,
    p_profile_id uuid,
    p_content_id uuid,
    p_space_id uuid,
    p_generation_id uuid,
    p_required_role text
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF jsonb_typeof(p_intent) <> 'object' THEN
        RETURN 'intent must be a JSON object';
    END IF;
    IF (p_intent ->> 'schema_version')::text IS DISTINCT FROM '1' THEN
        RETURN 'intent.schema_version must be 1';
    END IF;
    IF p_job_kind IN ('doc.extract', 'embed.chunks', 'transform.distill', 'transform.compact') THEN
        IF p_profile_id IS NULL THEN
            RETURN 'content processing jobs must pin an immutable profile';
        END IF;
        IF p_content_id IS NULL THEN
            RETURN 'content processing jobs must pin a canonical content revision';
        END IF;
    END IF;
    IF p_job_kind = 'doc.extract' AND p_space_id IS NULL THEN
        RETURN 'doc.extract must pin the embedding space whose chunking policy it uses';
    END IF;
    IF p_job_kind = 'embed.chunks' AND (p_space_id IS NULL OR p_generation_id IS NULL) THEN
        RETURN 'embed.chunks must pin a space and an index generation';
    END IF;
    IF p_job_kind IN ('transform.distill', 'transform.compact')
       AND jsonb_typeof(p_intent -> 'transform') <> 'object' THEN
        RETURN 'transform jobs must pin intent.transform identity and version';
    END IF;
    IF p_job_kind IN ('graph.code.extract', 'graph.memory.extract') THEN
        IF jsonb_typeof(p_intent -> 'pin') <> 'object' THEN
            RETURN 'graph jobs must pin intent.pin (repository/commit or snapshot identity)';
        END IF;
        IF p_required_role <> 'graph' THEN
            RETURN 'graph jobs run in the graph worker role and pin required_role graph';
        END IF;
    END IF;
    IF p_job_kind NOT IN ('graph.code.extract', 'graph.memory.extract')
       AND p_content_id IS NULL THEN
        RETURN 'only graph jobs may omit a canonical content revision';
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION cortex_processing.validate_job_intent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    violation text;
BEGIN
    violation := cortex_processing.job_intent_violation(
        NEW.job_kind, NEW.intent, NEW.profile_id, NEW.content_id,
        NEW.space_id, NEW.generation_id, NEW.required_role
    );
    IF violation IS NOT NULL THEN
        RAISE EXCEPTION '%', violation USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.validate_job_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.job_id <> OLD.job_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.installation_id <> OLD.installation_id
       OR NEW.job_kind <> OLD.job_kind
       OR NEW.dedupe_key <> OLD.dedupe_key
       OR NEW.requested_by_principal <> OLD.requested_by_principal
       OR NEW.created_at <> OLD.created_at
       OR NEW.content_id IS DISTINCT FROM OLD.content_id
       OR NEW.source_revision IS DISTINCT FROM OLD.source_revision
       OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
       OR NEW.space_id IS DISTINCT FROM OLD.space_id
       OR NEW.generation_id IS DISTINCT FROM OLD.generation_id
       OR NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
        RAISE EXCEPTION 'job identity and its pinned inputs are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.required_role <> OLD.required_role THEN
        -- A job may be rerouted once, when execution discovers the format
        -- needs a different executor role than the enqueue-time detection
        -- predicted. After that it waits visibly for that role.
        IF OLD.rerouted_at IS NOT NULL THEN
            RAISE EXCEPTION 'a job is rerouted to another executor role at most once'
                USING ERRCODE = '55000';
        END IF;
        NEW.rerouted_at := pg_catalog.now();
    END IF;
    IF NEW.fencing_epoch < OLD.fencing_epoch THEN
        RAISE EXCEPTION 'a fencing epoch never decreases' USING ERRCODE = '55000';
    END IF;
    IF OLD.cancel_requested AND NOT NEW.cancel_requested THEN
        RAISE EXCEPTION 'cancellation cannot be withdrawn' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('succeeded', 'cancelled', 'quarantined') THEN
        RAISE EXCEPTION 'a % job is terminal', OLD.status USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.status = 'queued' AND NEW.status IN ('leased', 'cancelled', 'blocked'))
        OR (OLD.status = 'leased'
            AND NEW.status IN ('leased', 'queued', 'succeeded', 'failed',
                               'cancelled', 'quarantined', 'blocked'))
        OR (OLD.status = 'blocked' AND NEW.status IN ('queued', 'cancelled'))
        OR (OLD.status = 'failed' AND NEW.status IN ('queued', 'cancelled'))
        OR OLD.status = NEW.status
    ) THEN
        RAISE EXCEPTION 'job transition % -> % is not allowed', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'leased'
       AND (OLD.status <> 'leased' OR NEW.lease_owner IS DISTINCT FROM OLD.lease_owner)
       AND NEW.fencing_epoch = OLD.fencing_epoch THEN
        RAISE EXCEPTION 'claiming a job must advance its fencing epoch'
            USING ERRCODE = '55000';
    END IF;
    -- An owner and an expiry are required; the expiry may already be in the
    -- past, because a lease expires by time passing and the reaper, the
    -- cancellation path and a forced reclaim all have to be able to write that
    -- row afterwards. Claiming still always sets a future expiry.
    IF NEW.status = 'leased'
       AND (NEW.lease_owner IS NULL OR NEW.lease_expires_at IS NULL) THEN
        RAISE EXCEPTION 'a lease needs an owner and an expiry' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> 'leased' AND NEW.lease_owner IS NOT NULL THEN
        RAISE EXCEPTION 'only a leased job holds a lease' USING ERRCODE = '23514';
    END IF;
    IF NEW.status IN ('succeeded', 'failed', 'cancelled', 'quarantined')
       AND NEW.finished_at IS NULL THEN
        NEW.finished_at := pg_catalog.now();
    END IF;
    IF NEW.status = 'succeeded' AND NEW.error_code IS NOT NULL THEN
        RAISE EXCEPTION 'a succeeded job records no error' USING ERRCODE = '23514';
    END IF;
    NEW.updated_at := pg_catalog.now();
    RETURN NEW;
END
$function$;

CREATE TRIGGER jobs_validate_intent
    BEFORE INSERT ON cortex_processing.jobs
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_job_intent();

CREATE TRIGGER jobs_validate_transition
    BEFORE UPDATE ON cortex_processing.jobs
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_job_transition();

CREATE TRIGGER jobs_reject_delete
    BEFORE DELETE ON cortex_processing.jobs
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

-- Workload-shaped queue indexes: a partial index for claimable work, one for
-- expired leases and one for scoped status listing with a stable pagination key.
CREATE INDEX jobs_ready
    ON cortex_processing.jobs(required_role, available_at, priority DESC, job_id)
    WHERE status = 'queued';
CREATE INDEX jobs_expired_leases
    ON cortex_processing.jobs(lease_expires_at, job_id)
    WHERE status = 'leased';
CREATE INDEX jobs_scope_status_created
    ON cortex_processing.jobs(scope_id, status, created_at DESC, job_id);
CREATE INDEX jobs_scope_role_status
    ON cortex_processing.jobs(scope_id, required_role, status)
    WHERE status IN ('queued', 'leased', 'blocked');
CREATE INDEX jobs_batch
    ON cortex_processing.jobs(batch_id, job_id)
    WHERE batch_id IS NOT NULL;

CREATE TABLE cortex_processing.attempts (
    attempt_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES cortex_processing.jobs(job_id) ON DELETE RESTRICT,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    fencing_epoch integer NOT NULL CHECK (fencing_epoch > 0),
    worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 128),
    worker_role text NOT NULL CHECK (worker_role IN ('doc', 'embed', 'graph')),
    status text NOT NULL DEFAULT 'leased' CHECK (status IN (
        'leased', 'running', 'succeeded', 'failed', 'cancelled',
        'expired', 'quarantined', 'blocked'
    )),
    claimed_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz NOT NULL,
    finished_at timestamptz,
    outcome_code text CHECK (outcome_code IS NULL OR length(outcome_code) BETWEEN 1 AND 64),
    outcome_detail text CHECK (outcome_detail IS NULL OR length(outcome_detail) <= 512),
    stats jsonb NOT NULL DEFAULT '{}'::jsonb,
    warnings text[] NOT NULL DEFAULT '{}',
    UNIQUE (job_id, fencing_epoch)
);

CREATE FUNCTION cortex_processing.validate_attempt_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.attempt_id <> OLD.attempt_id
       OR NEW.job_id <> OLD.job_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.fencing_epoch <> OLD.fencing_epoch
       OR NEW.worker_id <> OLD.worker_id
       OR NEW.worker_role <> OLD.worker_role
       OR NEW.claimed_at <> OLD.claimed_at THEN
        RAISE EXCEPTION 'attempt identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'cancelled', 'expired', 'quarantined', 'blocked') THEN
        RAISE EXCEPTION 'a % attempt is terminal', OLD.status USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.status = 'leased' AND NEW.status IN ('leased', 'running', 'succeeded', 'failed',
                                                 'cancelled', 'expired', 'quarantined', 'blocked'))
        OR (OLD.status = 'running' AND NEW.status IN ('running', 'succeeded', 'failed',
                                                     'cancelled', 'expired', 'quarantined', 'blocked'))
        OR OLD.status = NEW.status
    ) THEN
        RAISE EXCEPTION 'attempt transition % -> % is not allowed', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    IF NEW.heartbeat_at < OLD.heartbeat_at THEN
        RAISE EXCEPTION 'a heartbeat never moves backwards' USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'running' AND NEW.started_at IS NULL THEN
        NEW.started_at := pg_catalog.now();
    END IF;
    IF NEW.status IN ('succeeded', 'failed', 'cancelled', 'expired', 'quarantined', 'blocked')
       AND NEW.finished_at IS NULL THEN
        NEW.finished_at := pg_catalog.now();
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER attempts_validate_transition
    BEFORE UPDATE ON cortex_processing.attempts
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_attempt_transition();

CREATE TRIGGER attempts_reject_delete
    BEFORE DELETE ON cortex_processing.attempts
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX attempts_job_epoch
    ON cortex_processing.attempts(job_id, fencing_epoch DESC);
CREATE INDEX attempts_scope_claimed
    ON cortex_processing.attempts(scope_id, claimed_at DESC, attempt_id);

-- ---------------------------------------------------------------------------
-- Queue admission and in-flight budgets. Capacity is resolved from a scope
-- override or the seeded installation-agnostic default, and part of every
-- capacity is held back for interactive retrieval so a corpus backfill cannot
-- starve a worker asking questions.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.budget_defaults (
    resource text PRIMARY KEY CHECK (resource IN (
        'provider_calls', 'embedding_tokens', 'bytes_in_flight', 'concurrency'
    )),
    capacity bigint NOT NULL CHECK (capacity > 0),
    interactive_reserve bigint NOT NULL DEFAULT 0 CHECK (interactive_reserve >= 0)
);

INSERT INTO cortex_processing.budget_defaults (resource, capacity, interactive_reserve) VALUES
    ('provider_calls', 64, 8),
    ('embedding_tokens', 2000000, 100000),
    ('bytes_in_flight', 268435456, 16777216),
    ('concurrency', 8, 2);

CREATE TABLE cortex_processing.budget_policies (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    resource text NOT NULL CHECK (resource IN (
        'provider_calls', 'embedding_tokens', 'bytes_in_flight', 'concurrency'
    )),
    capacity bigint NOT NULL CHECK (capacity > 0),
    interactive_reserve bigint NOT NULL DEFAULT 0 CHECK (interactive_reserve >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, resource)
);

CREATE TABLE cortex_processing.budget_reservations (
    reservation_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    job_id uuid REFERENCES cortex_processing.jobs(job_id) ON DELETE RESTRICT,
    resource text NOT NULL CHECK (resource IN (
        'provider_calls', 'embedding_tokens', 'bytes_in_flight', 'concurrency'
    )),
    amount bigint NOT NULL CHECK (amount > 0),
    is_interactive boolean NOT NULL DEFAULT false,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    CHECK (released_at IS NULL OR released_at >= reserved_at)
);

-- One *active* reservation per job and resource. Released rows stay as the
-- audit trail, and a retried attempt reserves its in-flight slot again, so the
-- uniqueness must not cover released rows or every retry would fail admission.
CREATE UNIQUE INDEX budget_reservations_one_per_job_resource
    ON cortex_processing.budget_reservations(job_id, resource)
    WHERE job_id IS NOT NULL AND released_at IS NULL;
CREATE INDEX budget_reservations_active
    ON cortex_processing.budget_reservations(scope_id, resource)
    WHERE released_at IS NULL;

CREATE FUNCTION cortex_processing.budget_capacity(p_scope_id uuid, p_resource text)
RETURNS bigint
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
    SELECT COALESCE(
        (
            SELECT p.capacity
              FROM cortex_processing.budget_policies AS p
             WHERE p.scope_id = p_scope_id
               AND p.resource = p_resource
        ),
        (
            SELECT d.capacity
              FROM cortex_processing.budget_defaults AS d
             WHERE d.resource = p_resource
        )
    )
$function$;

CREATE FUNCTION cortex_processing.budget_interactive_reserve(
    p_scope_id uuid,
    p_resource text
)
RETURNS bigint
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
    SELECT COALESCE(
        (
            SELECT p.interactive_reserve
              FROM cortex_processing.budget_policies AS p
             WHERE p.scope_id = p_scope_id
               AND p.resource = p_resource
        ),
        (
            SELECT d.interactive_reserve
              FROM cortex_processing.budget_defaults AS d
             WHERE d.resource = p_resource
        )
    )
$function$;

CREATE FUNCTION cortex_processing.validate_budget_reservation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    capacity bigint;
    reserve bigint;
    in_use bigint;
    ceiling bigint;
BEGIN
    capacity := cortex_processing.budget_capacity(NEW.scope_id, NEW.resource);
    IF capacity IS NULL THEN
        RAISE EXCEPTION 'no budget capacity is configured for resource %', NEW.resource
            USING ERRCODE = '53400';
    END IF;
    reserve := COALESCE(
        cortex_processing.budget_interactive_reserve(NEW.scope_id, NEW.resource), 0
    );
    SELECT COALESCE(sum(r.amount), 0)
      INTO in_use
      FROM cortex_processing.budget_reservations AS r
     WHERE r.scope_id = NEW.scope_id
       AND r.resource = NEW.resource
       AND r.released_at IS NULL
       AND (NEW.is_interactive OR NOT r.is_interactive);
    -- Bulk work must leave the interactive reserve untouched; interactive work
    -- may consume the whole capacity but nothing beyond it.
    ceiling := CASE WHEN NEW.is_interactive THEN capacity ELSE capacity - reserve END;
    IF in_use + NEW.amount > ceiling THEN
        RAISE EXCEPTION
            'budget exhausted for %: % of % in use, % requested, % permitted',
            NEW.resource, in_use, capacity, NEW.amount, ceiling
            USING ERRCODE = '53400';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.validate_budget_release()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.reservation_id <> OLD.reservation_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.resource <> OLD.resource
       OR NEW.amount <> OLD.amount
       OR NEW.is_interactive <> OLD.is_interactive
       OR NEW.reserved_at <> OLD.reserved_at THEN
        RAISE EXCEPTION 'a budget reservation is immutable except for its release'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.released_at IS NOT NULL THEN
        RAISE EXCEPTION 'a reservation is released once' USING ERRCODE = '55000';
    END IF;
    IF NEW.released_at IS NULL THEN
        RAISE EXCEPTION 'a reservation update must release it' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER budget_reservations_validate_admission
    BEFORE INSERT ON cortex_processing.budget_reservations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_budget_reservation();

CREATE TRIGGER budget_reservations_validate_release
    BEFORE UPDATE ON cortex_processing.budget_reservations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_budget_release();

CREATE TRIGGER budget_reservations_reject_delete
    BEFORE DELETE ON cortex_processing.budget_reservations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE TRIGGER budget_policies_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.budget_policies
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE TRIGGER budget_defaults_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.budget_defaults
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

-- ---------------------------------------------------------------------------
-- Projections: chunk revisions bound to canonical content revisions, and the
-- vectors of one chunk in one index generation. The vector column has NO fixed
-- dimension; per-row dimension equality with the owning space is enforced by a
-- trigger, so incompatible geometries can never share an index and no
-- fixed-dimension query cast is the compatibility test (F07).
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.chunk_revisions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    content_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    space_id uuid NOT NULL REFERENCES cortex_processing.embedding_spaces(space_id),
    chunk_ordinal integer NOT NULL CHECK (chunk_ordinal >= 0),
    chunk_id uuid NOT NULL,
    chunking_policy text NOT NULL CHECK (length(chunking_policy) BETWEEN 1 AND 96),
    text_sha256 bytea NOT NULL CHECK (octet_length(text_sha256) = 32),
    chunk_text text NOT NULL CHECK (length(chunk_text) BETWEEN 1 AND 65536),
    span jsonb NOT NULL,
    block_spans jsonb NOT NULL DEFAULT '[]'::jsonb,
    block_kinds text[] NOT NULL DEFAULT '{}',
    heading_path text[] NOT NULL DEFAULT '{}',
    format_id text CHECK (format_id IS NULL OR length(format_id) BETWEEN 1 AND 32),
    executor text CHECK (executor IS NULL OR length(executor) BETWEEN 1 AND 96),
    job_id uuid REFERENCES cortex_processing.jobs(job_id),
    truncated boolean NOT NULL DEFAULT false,
    warnings text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, content_id, revision, space_id, chunk_ordinal),
    UNIQUE (chunk_id),
    FOREIGN KEY (scope_id, content_id, revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_processing.span_violation(p_span jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF jsonb_typeof(p_span) <> 'object' THEN
        RETURN 'span must be a JSON object';
    END IF;
    IF jsonb_typeof(p_span -> 'start') <> 'number'
       OR jsonb_typeof(p_span -> 'end') <> 'number' THEN
        RETURN 'span.start and span.end must be numbers';
    END IF;
    IF (p_span ->> 'start')::bigint < 0
       OR (p_span ->> 'end')::bigint < (p_span ->> 'start')::bigint THEN
        RETURN 'span offsets must be ordered and non-negative';
    END IF;
    IF length(COALESCE(p_span ->> 'kind', 'text_offset')) > 32 THEN
        RETURN 'span.kind is too long';
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION cortex_processing.validate_chunk_revision()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    violation text;
    policy_identity text;
BEGIN
    violation := cortex_processing.span_violation(NEW.span);
    IF violation IS NOT NULL THEN
        RAISE EXCEPTION '%', violation USING ERRCODE = '23514';
    END IF;
    IF jsonb_typeof(NEW.block_spans) <> 'array' THEN
        RAISE EXCEPTION 'block_spans must be a JSON array' USING ERRCODE = '23514';
    END IF;
    IF NEW.chunk_text ~ E'\\x00' THEN
        RAISE EXCEPTION 'chunk text must not contain a NUL character' USING ERRCODE = '23514';
    END IF;
    -- The chunking policy recorded on the row must be the one pinned by the
    -- space, so two chunk policies can never coexist inside one space.
    SELECT (s.chunking ->> 'policy_id') || '@' || (s.chunking ->> 'version')
      INTO policy_identity
      FROM cortex_processing.embedding_spaces AS s
     WHERE s.space_id = NEW.space_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a chunk revision requires its embedding space' USING ERRCODE = '23503';
    END IF;
    IF NEW.chunking_policy <> policy_identity THEN
        RAISE EXCEPTION
            'chunking policy % does not match the space policy %',
            NEW.chunking_policy, policy_identity
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER chunk_revisions_validate
    BEFORE INSERT ON cortex_processing.chunk_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_chunk_revision();

CREATE TRIGGER chunk_revisions_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.chunk_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX chunk_revisions_space_scope
    ON cortex_processing.chunk_revisions(space_id, scope_id, content_id, revision);
CREATE INDEX chunk_revisions_scope_created
    ON cortex_processing.chunk_revisions(scope_id, created_at DESC, chunk_id);

CREATE TABLE cortex_processing.chunk_vectors (
    chunk_id uuid NOT NULL REFERENCES cortex_processing.chunk_revisions(chunk_id)
        ON DELETE RESTRICT,
    generation_id uuid NOT NULL REFERENCES cortex_processing.index_generations(generation_id)
        ON DELETE RESTRICT,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    space_id uuid NOT NULL REFERENCES cortex_processing.embedding_spaces(space_id),
    content_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    embedding public.vector NOT NULL,
    provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    model_revision text CHECK (model_revision IS NULL OR length(model_revision) BETWEEN 1 AND 128),
    normalization text NOT NULL CHECK (normalization IN ('l2', 'none')),
    job_id uuid REFERENCES cortex_processing.jobs(job_id),
    attempt_epoch integer NOT NULL CHECK (attempt_epoch > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, generation_id)
);

CREATE FUNCTION cortex_processing.validate_chunk_vector()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
DECLARE
    space_row cortex_processing.embedding_spaces%ROWTYPE;
    generation_row cortex_processing.index_generations%ROWTYPE;
    chunk_row cortex_processing.chunk_revisions%ROWTYPE;
    squared_norm double precision;
BEGIN
    SELECT * INTO space_row
      FROM cortex_processing.embedding_spaces
     WHERE space_id = NEW.space_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a vector requires its embedding space' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO generation_row
      FROM cortex_processing.index_generations
     WHERE generation_id = NEW.generation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a vector requires its index generation' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO chunk_row
      FROM cortex_processing.chunk_revisions
     WHERE chunk_id = NEW.chunk_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a vector requires its chunk revision' USING ERRCODE = '23503';
    END IF;

    IF generation_row.space_id <> NEW.space_id THEN
        RAISE EXCEPTION 'a vector cannot mix embedding spaces' USING ERRCODE = '23514';
    END IF;
    IF generation_row.state IN ('retired', 'failed') THEN
        RAISE EXCEPTION 'generation % is % and cannot accept vectors',
            generation_row.generation, generation_row.state
            USING ERRCODE = '55000';
    END IF;
    IF chunk_row.space_id <> NEW.space_id
       OR chunk_row.scope_id <> NEW.scope_id
       OR chunk_row.content_id <> NEW.content_id
       OR chunk_row.revision <> NEW.revision THEN
        RAISE EXCEPTION 'a vector must match the identity of its chunk revision'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.provider <> space_row.provider OR NEW.model_id <> space_row.model_id THEN
        RAISE EXCEPTION
            'provider % model % does not match the immutable space identity % %',
            NEW.provider, NEW.model_id, space_row.provider, space_row.model_id
            USING ERRCODE = '23514';
    END IF;
    IF NEW.normalization <> space_row.normalization THEN
        RAISE EXCEPTION 'vector normalization does not match its space' USING ERRCODE = '23514';
    END IF;
    -- The compatibility test is the recorded space dimension, never a cast.
    IF public.vector_dims(NEW.embedding) <> space_row.dimensions THEN
        RAISE EXCEPTION
            'vector dimension % does not match embedding space dimension %',
            public.vector_dims(NEW.embedding), space_row.dimensions
            USING ERRCODE = '23514';
    END IF;
    IF space_row.normalization = 'l2' THEN
        -- <#> is the negative inner product, so this is the squared L2 norm.
        squared_norm := -(NEW.embedding OPERATOR(public.<#>) NEW.embedding);
        IF abs(sqrt(squared_norm) - 1.0) > 0.0001 THEN
            RAISE EXCEPTION
                'an l2-normalized space requires unit-norm vectors, got %',
                sqrt(squared_norm)
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER chunk_vectors_validate
    BEFORE INSERT ON cortex_processing.chunk_vectors
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_chunk_vector();

CREATE TRIGGER chunk_vectors_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.chunk_vectors
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX chunk_vectors_space_generation
    ON cortex_processing.chunk_vectors(space_id, generation_id, scope_id);
CREATE INDEX chunk_vectors_scope_content
    ON cortex_processing.chunk_vectors(scope_id, content_id, revision, generation_id);

-- No ANN index is created here on purpose: HNSW/IVFFlat require a fixed
-- dimension, and the only way to index a dimensionless column is an expression
-- cast which would then also have to appear in the query path. That is exactly
-- the F07 accident this schema avoids. Candidate retrieval is an exact scan of
-- one active generation bounded by p_limit; a per-generation ANN index is an
-- explicit Operations decision, not a silent default.

-- ---------------------------------------------------------------------------
-- Optional derived transforms (R18). Distillation and compaction live here as
-- new rows and never touch canonical content: Processing must not edit original
-- memory based on model output.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.distillations (
    distillation_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    content_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    transform_kind text NOT NULL CHECK (transform_kind IN ('distill', 'compact')),
    transform_identity text NOT NULL CHECK (length(transform_identity) BETWEEN 1 AND 96),
    profile_id uuid NOT NULL REFERENCES cortex_processing.processing_profiles(profile_id),
    output_text text NOT NULL CHECK (length(output_text) BETWEEN 1 AND 65536),
    output_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    commitments text[] NOT NULL DEFAULT '{}',
    coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    job_id uuid REFERENCES cortex_processing.jobs(job_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_id, content_id, revision, transform_identity),
    FOREIGN KEY (scope_id, content_id, revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT
);

CREATE TRIGGER distillations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.distillations
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX distillations_scope_content
    ON cortex_processing.distillations(scope_id, content_id, revision);

-- ---------------------------------------------------------------------------
-- Quarantine ledger: permanent parse/schema failures keep their original
-- payload, hash and reason until an operator disposes of them.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.quarantine_ledger (
    quarantine_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    job_id uuid REFERENCES cortex_processing.jobs(job_id) ON DELETE RESTRICT,
    content_id uuid,
    revision integer,
    reason_code text NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    reason_detail text CHECK (reason_detail IS NULL OR length(reason_detail) <= 512),
    preserved_payload jsonb NOT NULL,
    payload_sha256 bytea NOT NULL CHECK (octet_length(payload_sha256) = 32),
    executor text CHECK (executor IS NULL OR length(executor) BETWEEN 1 AND 96),
    format_id text CHECK (format_id IS NULL OR length(format_id) BETWEEN 1 AND 32),
    disposition text NOT NULL DEFAULT 'review'
        CHECK (disposition IN ('review', 'released', 'rejected', 'rebuilt')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_by_principal uuid REFERENCES cortex_auth.principals(principal_id),
    UNIQUE NULLS NOT DISTINCT (scope_id, job_id, reason_code)
);

CREATE FUNCTION cortex_processing.validate_quarantine_resolution()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.disposition = OLD.disposition THEN
        RAISE EXCEPTION 'a quarantine entry is immutable except for its disposition'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.disposition <> 'review' THEN
        RAISE EXCEPTION 'a resolved quarantine entry cannot change again' USING ERRCODE = '55000';
    END IF;
    IF NEW.resolved_at IS NULL THEN
        NEW.resolved_at := pg_catalog.now();
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER quarantine_validate_resolution
    BEFORE UPDATE ON cortex_processing.quarantine_ledger
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_quarantine_resolution();

CREATE TRIGGER quarantine_reject_delete
    BEFORE DELETE ON cortex_processing.quarantine_ledger
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX quarantine_scope_created
    ON cortex_processing.quarantine_ledger(scope_id, created_at DESC, quarantine_id);
CREATE INDEX quarantine_open
    ON cortex_processing.quarantine_ledger(scope_id, reason_code)
    WHERE disposition = 'review';

-- ---------------------------------------------------------------------------
-- Worker heartbeats. A missing executor role is an observable fact, not a
-- silent gap: coverage and job status report work waiting for an executor that
-- has not registered a fresh heartbeat (R12).
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.workers (
    worker_id text PRIMARY KEY CHECK (length(worker_id) BETWEEN 1 AND 128),
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    worker_role text NOT NULL CHECK (worker_role IN ('doc', 'embed', 'graph')),
    executor_roles text[] NOT NULL DEFAULT '{}',
    handler_kinds text[] NOT NULL DEFAULT '{}',
    package_version text NOT NULL CHECK (length(package_version) BETWEEN 1 AND 32),
    started_at timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    stopped_at timestamptz,
    stats jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE FUNCTION cortex_processing.validate_worker_identity()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.worker_id <> OLD.worker_id
           OR NEW.installation_id <> OLD.installation_id
           OR NEW.principal_id <> OLD.principal_id
           OR NEW.worker_role <> OLD.worker_role
           OR NEW.started_at <> OLD.started_at THEN
            RAISE EXCEPTION 'worker identity is immutable' USING ERRCODE = '55000';
        END IF;
        IF NEW.last_heartbeat_at < OLD.last_heartbeat_at THEN
            RAISE EXCEPTION 'a worker heartbeat never moves backwards' USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER workers_validate_identity
    BEFORE INSERT OR UPDATE ON cortex_processing.workers
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_worker_identity();

CREATE TRIGGER workers_reject_delete
    BEFORE DELETE ON cortex_processing.workers
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE INDEX workers_role_heartbeat
    ON cortex_processing.workers(worker_role, last_heartbeat_at DESC);

-- ---------------------------------------------------------------------------
-- Transactional outbox for Processing facts. The column contract matches
-- cortex_coord.outbox_events exactly, so the feed dispatcher can register this
-- table as one more source:
--   table                        = cortex_processing.outbox_events
--   aggregate_kind_expression    = aggregate_kind
--   aggregate_id_expression      = aggregate_id
--   version_expression           = (payload->>'aggregate_version')::bigint
-- Every event is committed in the same transaction as the state change it
-- describes. Unlike the coordination outbox, inserts do NOT require a declared
-- writer-policy revision: these are derived-projection facts whose authority is
-- the writer's own scope grant, not the canonical content writer policy.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.outbox_events (
    event_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    aggregate_kind text NOT NULL CHECK (aggregate_kind IN (
        'processing_job', 'embedding_space', 'index_generation'
    )),
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

CREATE INDEX processing_outbox_scope_created
    ON cortex_processing.outbox_events(scope_id, created_at, event_id);
CREATE INDEX processing_outbox_undelivered
    ON cortex_processing.outbox_events(scope_id, created_at)
    WHERE delivered_at IS NULL;

CREATE FUNCTION cortex_processing.validate_outbox_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF NEW.delivered_at IS NOT NULL THEN
        RAISE EXCEPTION 'outbox events start undelivered' USING ERRCODE = '23514';
    END IF;
    IF jsonb_typeof(NEW.payload) <> 'object'
       OR (NEW.payload ->> 'aggregate_version') !~ '^[0-9]{1,19}$' THEN
        RAISE EXCEPTION
            'outbox payload must carry a numeric aggregate_version for feed ordering'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_processing.validate_outbox_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
BEGIN
    IF OLD.delivered_at IS NOT NULL OR NEW.delivered_at IS NULL THEN
        RAISE EXCEPTION 'outbox delivery is marked exactly once' USING ERRCODE = '55000';
    END IF;
    IF NEW.event_id <> OLD.event_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.aggregate_kind <> OLD.aggregate_kind
       OR NEW.aggregate_id <> OLD.aggregate_id
       OR NEW.event_type <> OLD.event_type
       OR NEW.payload <> OLD.payload
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'outbox events are immutable apart from delivery'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER processing_outbox_validate_insert
    BEFORE INSERT ON cortex_processing.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_outbox_insert();

CREATE TRIGGER processing_outbox_validate_update
    BEFORE UPDATE ON cortex_processing.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.validate_outbox_update();

CREATE TRIGGER processing_outbox_reject_delete
    BEFORE DELETE ON cortex_processing.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

-- ---------------------------------------------------------------------------
-- Central provider-role configuration (R24) and local-inference controls (R25).
-- These are installation registries: the application role reads them, and only
-- the migrator/operations lane writes them. They store a credential REFERENCE
-- (an environment or secret-file name), never a credential value.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_processing.provider_roles (
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    role text NOT NULL CHECK (role IN ('embedding', 'rerank', 'analysis')),
    revision integer NOT NULL CHECK (revision > 0),
    provider_kind text NOT NULL
        CHECK (provider_kind IN ('hash-local', 'openai-compatible', 'ollama', 'none')),
    base_url text CHECK (base_url IS NULL OR length(base_url) BETWEEN 8 AND 512),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    credential_ref text CHECK (credential_ref IS NULL OR length(credential_ref) BETWEEN 1 AND 128),
    egress_allowlist text[] NOT NULL DEFAULT '{}',
    timeouts jsonb NOT NULL DEFAULT '{}'::jsonb,
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, role, revision),
    CHECK (provider_kind IN ('hash-local', 'none') OR base_url IS NOT NULL),
    CHECK (provider_kind <> 'openai-compatible' OR credential_ref IS NOT NULL)
);

CREATE TABLE cortex_processing.local_inference_policies (
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    revision integer NOT NULL CHECK (revision > 0),
    routing_policy text NOT NULL
        CHECK (routing_policy IN ('local_only', 'remote_allowed', 'disabled')),
    max_resident_models integer NOT NULL CHECK (max_resident_models BETWEEN 0 AND 64),
    concurrency_cpu integer NOT NULL CHECK (concurrency_cpu BETWEEN 1 AND 256),
    concurrency_gpu integer NOT NULL CHECK (concurrency_gpu BETWEEN 0 AND 256),
    batch_limit integer NOT NULL CHECK (batch_limit BETWEEN 1 AND 4096),
    idle_unload_seconds integer NOT NULL CHECK (idle_unload_seconds BETWEEN 0 AND 86400),
    offline boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, revision)
);

CREATE TABLE cortex_processing.local_model_manifests (
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    role text NOT NULL CHECK (role IN ('embedding', 'rerank', 'analysis')),
    digest text NOT NULL CHECK (digest ~ '^[A-Za-z0-9][A-Za-z0-9._+:-]{7,255}$'),
    license text NOT NULL CHECK (length(license) BETWEEN 1 AND 128),
    platform text NOT NULL CHECK (length(platform) BETWEEN 1 AND 128),
    dimensions integer CHECK (dimensions IS NULL OR dimensions BETWEEN 1 AND 16000),
    status text NOT NULL DEFAULT 'declared'
        CHECK (status IN ('declared', 'installed', 'activated', 'retired')),
    installed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, model_id, role),
    CHECK (status <> 'installed' OR installed_at IS NOT NULL)
);

CREATE FUNCTION cortex_processing.active_provider_role(p_role text)
RETURNS TABLE (
    installation_id uuid,
    role text,
    revision integer,
    provider_kind text,
    base_url text,
    model_id text,
    credential_ref text,
    egress_allowlist text[],
    timeouts jsonb,
    capabilities jsonb,
    enabled boolean
)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
    SELECT r.installation_id, r.role, r.revision, r.provider_kind, r.base_url,
           r.model_id, r.credential_ref, r.egress_allowlist, r.timeouts,
           r.capabilities, r.enabled
      FROM cortex_processing.provider_roles AS r
     WHERE r.role = p_role
       AND r.revision = (
           SELECT max(latest.revision)
             FROM cortex_processing.provider_roles AS latest
            WHERE latest.role = p_role
              AND latest.installation_id = r.installation_id
       )
$function$;

CREATE FUNCTION cortex_processing.active_local_inference_policy()
RETURNS TABLE (
    installation_id uuid,
    revision integer,
    routing_policy text,
    max_resident_models integer,
    concurrency_cpu integer,
    concurrency_gpu integer,
    batch_limit integer,
    idle_unload_seconds integer,
    offline boolean
)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
    SELECT p.installation_id, p.revision, p.routing_policy, p.max_resident_models,
           p.concurrency_cpu, p.concurrency_gpu, p.batch_limit,
           p.idle_unload_seconds, p.offline
      FROM cortex_processing.local_inference_policies AS p
     WHERE p.revision = (
           SELECT max(latest.revision)
             FROM cortex_processing.local_inference_policies AS latest
            WHERE latest.installation_id = p.installation_id
       )
$function$;

CREATE TRIGGER provider_roles_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.provider_roles
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE TRIGGER local_inference_policies_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.local_inference_policies
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

CREATE TRIGGER local_model_manifests_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_processing.local_model_manifests
    FOR EACH ROW EXECUTE FUNCTION cortex_processing.reject_processing_mutation();

-- ---------------------------------------------------------------------------
-- Privileged registry commands. Creating a space or switching retrieval routing
-- changes what every later query means, so both require the installation owner
-- and run through narrow SECURITY DEFINER functions with a fixed search path,
-- no dynamic SQL and an explicit privileged-action audit row — the same shape
-- as cortex_core.register_source_connector in 0002.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_processing.create_embedding_space(
    p_caller_principal_id uuid,
    p_space_name text,
    p_provider text,
    p_model_id text,
    p_model_revision text,
    p_dimensions integer,
    p_normalization text,
    p_metric text,
    p_query_prefix text,
    p_document_prefix text,
    p_chunking jsonb
)
RETURNS TABLE (space_id uuid, generation_id uuid, generation integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, cortex_processing, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    new_space_id uuid := gen_random_uuid();
    new_generation_id uuid := gen_random_uuid();
    violation text;
BEGIN
    SELECT installation_id
      INTO caller_installation
      FROM cortex_auth.principals
     WHERE principal_id = p_caller_principal_id
       AND status = 'active';
    PERFORM cortex_auth.require_installation_owner(p_caller_principal_id, caller_installation);

    IF length(p_space_name) NOT BETWEEN 1 AND 96 THEN
        RAISE EXCEPTION 'space name must be 1..96 characters' USING ERRCODE = '23514';
    END IF;
    IF p_dimensions NOT BETWEEN 1 AND 16000 THEN
        RAISE EXCEPTION 'dimensions must be between 1 and 16000' USING ERRCODE = '23514';
    END IF;
    IF p_normalization NOT IN ('l2', 'none') THEN
        RAISE EXCEPTION 'normalization must be l2 or none' USING ERRCODE = '23514';
    END IF;
    IF p_metric NOT IN ('cosine', 'l2', 'inner_product') THEN
        RAISE EXCEPTION 'metric must be cosine, l2 or inner_product' USING ERRCODE = '23514';
    END IF;
    IF jsonb_typeof(p_chunking) <> 'object'
       OR (p_chunking ->> 'schema_version')::text IS DISTINCT FROM '1'
       OR length(COALESCE(p_chunking ->> 'policy_id', '')) NOT BETWEEN 1 AND 64
       OR (p_chunking ->> 'version') !~ '^[0-9]{1,6}$'
       OR p_chunking ->> 'kind' NOT IN ('structural', 'token_window') THEN
        RAISE EXCEPTION 'chunking policy is invalid' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM cortex_processing.embedding_spaces
         WHERE installation_id = caller_installation
           AND space_name = p_space_name
    ) THEN
        RAISE EXCEPTION 'an embedding space with that name already exists'
            USING ERRCODE = '23505';
    END IF;

    INSERT INTO cortex_processing.embedding_spaces (
        space_id, installation_id, space_name, provider, model_id, model_revision,
        dimensions, normalization, metric, query_prefix, document_prefix, chunking,
        active_generation_id, created_by_principal
    ) VALUES (
        new_space_id, caller_installation, p_space_name, p_provider, p_model_id,
        p_model_revision, p_dimensions, p_normalization, p_metric, p_query_prefix,
        p_document_prefix, p_chunking, new_generation_id, p_caller_principal_id
    );

    INSERT INTO cortex_processing.index_generations (
        generation_id, space_id, installation_id, generation, state, chunking,
        created_by_principal, activated_at
    ) VALUES (
        new_generation_id, new_space_id, caller_installation, 1, 'active', p_chunking,
        p_caller_principal_id, pg_catalog.now()
    );

    INSERT INTO cortex_auth.privileged_actions (
        installation_id, caller_principal_id, action_type, detail
    ) VALUES (
        caller_installation, p_caller_principal_id, 'create_embedding_space',
        jsonb_build_object(
            'space_id', new_space_id, 'space_name', p_space_name,
            'provider', p_provider, 'model_id', p_model_id,
            'dimensions', p_dimensions, 'metric', p_metric,
            'normalization', p_normalization, 'generation_id', new_generation_id
        )
    );
    RETURN QUERY SELECT new_space_id, new_generation_id, 1;
END
$function$;

CREATE FUNCTION cortex_processing.begin_index_generation(
    p_caller_principal_id uuid,
    p_space_id uuid,
    p_profile_id uuid
)
RETURNS TABLE (generation_id uuid, generation integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, cortex_processing, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    space_installation uuid;
    space_chunking jsonb;
    next_generation integer;
    new_generation_id uuid := gen_random_uuid();
BEGIN
    SELECT installation_id
      INTO caller_installation
      FROM cortex_auth.principals
     WHERE principal_id = p_caller_principal_id
       AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'caller principal is not active' USING ERRCODE = '28000';
    END IF;
    SELECT installation_id, chunking
      INTO space_installation, space_chunking
      FROM cortex_processing.embedding_spaces
     WHERE space_id = p_space_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'embedding space is unavailable' USING ERRCODE = '22023';
    END IF;
    IF space_installation <> caller_installation THEN
        RAISE EXCEPTION 'embedding space belongs to another installation'
            USING ERRCODE = '28000';
    END IF;
    SELECT COALESCE(max(g.generation), 0) + 1
      INTO next_generation
      FROM cortex_processing.index_generations AS g
     WHERE g.space_id = p_space_id;

    INSERT INTO cortex_processing.index_generations (
        generation_id, space_id, installation_id, generation, state, chunking,
        profile_id, created_by_principal
    ) VALUES (
        new_generation_id, p_space_id, space_installation, next_generation,
        'building', space_chunking, p_profile_id, p_caller_principal_id
    );

    INSERT INTO cortex_auth.privileged_actions (
        installation_id, caller_principal_id, action_type, detail
    ) VALUES (
        caller_installation, p_caller_principal_id, 'begin_index_generation',
        jsonb_build_object(
            'space_id', p_space_id, 'generation_id', new_generation_id,
            'generation', next_generation
        )
    );
    RETURN QUERY SELECT new_generation_id, next_generation;
END
$function$;

CREATE FUNCTION cortex_processing.activate_index_generation(
    p_caller_principal_id uuid,
    p_space_id uuid,
    p_generation_id uuid
)
RETURNS TABLE (space_id uuid, generation_id uuid, generation integer, state text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, cortex_processing, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    target cortex_processing.index_generations%ROWTYPE;
    previous uuid;
BEGIN
    SELECT installation_id
      INTO caller_installation
      FROM cortex_auth.principals
     WHERE principal_id = p_caller_principal_id
       AND status = 'active';
    PERFORM cortex_auth.require_installation_owner(p_caller_principal_id, caller_installation);

    SELECT g.* INTO target
      FROM cortex_processing.index_generations AS g
     WHERE g.generation_id = p_generation_id
       AND g.space_id = p_space_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'index generation is unavailable for this space'
            USING ERRCODE = '22023';
    END IF;
    IF target.installation_id <> caller_installation THEN
        RAISE EXCEPTION 'embedding space belongs to another installation'
            USING ERRCODE = '28000';
    END IF;
    IF target.state NOT IN ('built', 'building', 'active') THEN
        RAISE EXCEPTION 'a % generation cannot be activated', target.state
            USING ERRCODE = '55000';
    END IF;

    SELECT g.generation_id
      INTO previous
      FROM cortex_processing.index_generations AS g
     WHERE g.space_id = p_space_id
       AND g.state = 'active'
       AND g.generation_id <> p_generation_id;
    IF previous IS NOT NULL THEN
        UPDATE cortex_processing.index_generations AS g
           SET state = 'retired'
         WHERE g.generation_id = previous;
    END IF;
    IF target.state <> 'active' THEN
        UPDATE cortex_processing.index_generations AS g
           SET state = 'active'
         WHERE g.generation_id = p_generation_id;
    END IF;
    UPDATE cortex_processing.embedding_spaces AS s
       SET active_generation_id = p_generation_id
     WHERE s.space_id = p_space_id;

    INSERT INTO cortex_auth.privileged_actions (
        installation_id, caller_principal_id, action_type, detail
    ) VALUES (
        caller_installation, p_caller_principal_id, 'activate_index_generation',
        jsonb_build_object(
            'space_id', p_space_id, 'generation_id', p_generation_id,
            'generation', target.generation, 'retired_generation_id', previous
        )
    );
    RETURN QUERY SELECT p_space_id, p_generation_id, target.generation, 'active'::text;
END
$function$;

-- ---------------------------------------------------------------------------
-- The retrieval contract. SECURITY INVOKER on purpose: row level security on
-- cortex_processing.chunk_vectors and cortex_processing.embedding_spaces still
-- applies, so a caller only ever sees candidates in its authorized read scopes.
-- Only the active generation is searched, dimensions are compared against the
-- recorded space dimension, and no fixed-dimension cast appears anywhere.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_processing.vector_candidates(
    p_space_id uuid,
    p_query public.vector,
    p_limit integer
)
RETURNS TABLE (
    content_id uuid,
    revision integer,
    chunk_id uuid,
    distance double precision
)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, cortex_processing, public, pg_temp
AS $function$
DECLARE
    space_dimensions integer;
    space_metric text;
    active_generation uuid;
BEGIN
    IF p_query IS NULL THEN
        RAISE EXCEPTION 'a query vector is required' USING ERRCODE = '22023';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'candidate limit must be between 1 and 1000'
            USING ERRCODE = '22023';
    END IF;
    SELECT s.dimensions, s.metric, g.generation_id
      INTO space_dimensions, space_metric, active_generation
      FROM cortex_processing.embedding_spaces AS s
      LEFT JOIN cortex_processing.index_generations AS g
             ON g.space_id = s.space_id
            AND g.state = 'active'
     WHERE s.space_id = p_space_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'embedding space is unavailable' USING ERRCODE = '22023';
    END IF;
    IF active_generation IS NULL THEN
        RAISE EXCEPTION 'embedding space has no active generation' USING ERRCODE = '22023';
    END IF;
    IF public.vector_dims(p_query) <> space_dimensions THEN
        RAISE EXCEPTION
            'query vector dimension % does not match embedding space dimension %',
            public.vector_dims(p_query), space_dimensions
            USING ERRCODE = '23514';
    END IF;

    IF space_metric = 'cosine' THEN
        RETURN QUERY
        SELECT v.content_id, v.revision, v.chunk_id,
               (v.embedding <=> p_query)::double precision
          FROM cortex_processing.chunk_vectors AS v
         WHERE v.space_id = p_space_id
           AND v.generation_id = active_generation
         ORDER BY v.embedding <=> p_query
         LIMIT p_limit;
    ELSIF space_metric = 'l2' THEN
        RETURN QUERY
        SELECT v.content_id, v.revision, v.chunk_id,
               (v.embedding <-> p_query)::double precision
          FROM cortex_processing.chunk_vectors AS v
         WHERE v.space_id = p_space_id
           AND v.generation_id = active_generation
         ORDER BY v.embedding <-> p_query
         LIMIT p_limit;
    ELSE
        RETURN QUERY
        SELECT v.content_id, v.revision, v.chunk_id,
               (v.embedding <#> p_query)::double precision
          FROM cortex_processing.chunk_vectors AS v
         WHERE v.space_id = p_space_id
           AND v.generation_id = active_generation
         ORDER BY v.embedding <#> p_query
         LIMIT p_limit;
    END IF;
END
$function$;

CREATE FUNCTION cortex_processing.space_state(p_space_id uuid)
RETURNS TABLE (
    space_id uuid,
    dimensions integer,
    metric text,
    normalization text,
    provider text,
    model_id text,
    model_revision text,
    active_generation_id uuid,
    active_generation_number integer,
    state text
)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_processing, pg_temp
AS $function$
    SELECT s.space_id, s.dimensions, s.metric, s.normalization, s.provider,
           s.model_id, s.model_revision, g.generation_id, g.generation,
           COALESCE(g.state, 'no_active_generation')
      FROM cortex_processing.embedding_spaces AS s
      LEFT JOIN cortex_processing.index_generations AS g
             ON g.generation_id = s.active_generation_id
     WHERE s.space_id = p_space_id
$function$;

-- ---------------------------------------------------------------------------
-- Row level security. Every table is forced, so even the owner of the runtime
-- credential is filtered. Scope-owned projections reuse the 0001 scope policy
-- helpers; installation registries reuse caller_installation_matches from 0002.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_processing.processing_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.processing_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY processing_profiles_read ON cortex_processing.processing_profiles
    FOR SELECT TO cortex_v2_app
    USING (
        NULLIF(current_setting('cortex.principal_id', true), '') IS NOT NULL
        AND (
            installation_id IS NULL
            OR cortex_core.caller_installation_matches(installation_id)
        )
    );
CREATE POLICY processing_profiles_migrator_all ON cortex_processing.processing_profiles
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.embedding_spaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.embedding_spaces FORCE ROW LEVEL SECURITY;
CREATE POLICY embedding_spaces_read ON cortex_processing.embedding_spaces
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY embedding_spaces_migrator_all ON cortex_processing.embedding_spaces
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.index_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.index_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY index_generations_read ON cortex_processing.index_generations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY index_generations_build_update ON cortex_processing.index_generations
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id))
    WITH CHECK (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY index_generations_migrator_all ON cortex_processing.index_generations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_scope_read ON cortex_processing.jobs
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY jobs_scope_insert ON cortex_processing.jobs
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND requested_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
        AND cortex_core.caller_installation_matches(installation_id)
    );
CREATE POLICY jobs_scope_update ON cortex_processing.jobs
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY jobs_migrator_all ON cortex_processing.jobs
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY attempts_scope_read ON cortex_processing.attempts
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY attempts_scope_insert ON cortex_processing.attempts
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY attempts_scope_update ON cortex_processing.attempts
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY attempts_migrator_all ON cortex_processing.attempts
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.budget_defaults ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.budget_defaults FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_defaults_read ON cortex_processing.budget_defaults
    FOR SELECT TO cortex_v2_app
    USING (NULLIF(current_setting('cortex.principal_id', true), '') IS NOT NULL);
CREATE POLICY budget_defaults_migrator_all ON cortex_processing.budget_defaults
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.budget_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.budget_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_policies_read ON cortex_processing.budget_policies
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY budget_policies_migrator_all ON cortex_processing.budget_policies
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.budget_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_reservations_read ON cortex_processing.budget_reservations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY budget_reservations_insert ON cortex_processing.budget_reservations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY budget_reservations_update ON cortex_processing.budget_reservations
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY budget_reservations_migrator_all ON cortex_processing.budget_reservations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.chunk_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.chunk_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY chunk_revisions_scope_read ON cortex_processing.chunk_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY chunk_revisions_scope_insert ON cortex_processing.chunk_revisions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY chunk_revisions_migrator_all ON cortex_processing.chunk_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.chunk_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.chunk_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY chunk_vectors_scope_read ON cortex_processing.chunk_vectors
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY chunk_vectors_scope_insert ON cortex_processing.chunk_vectors
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY chunk_vectors_migrator_all ON cortex_processing.chunk_vectors
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.distillations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.distillations FORCE ROW LEVEL SECURITY;
CREATE POLICY distillations_scope_read ON cortex_processing.distillations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY distillations_scope_insert ON cortex_processing.distillations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY distillations_migrator_all ON cortex_processing.distillations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.quarantine_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.quarantine_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY quarantine_scope_read ON cortex_processing.quarantine_ledger
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY quarantine_scope_insert ON cortex_processing.quarantine_ledger
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY quarantine_migrator_all ON cortex_processing.quarantine_ledger
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.workers ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.workers FORCE ROW LEVEL SECURITY;
CREATE POLICY workers_installation_read ON cortex_processing.workers
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY workers_self_insert ON cortex_processing.workers
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.caller_installation_matches(installation_id)
        AND principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
    );
CREATE POLICY workers_self_update ON cortex_processing.workers
    FOR UPDATE TO cortex_v2_app
    USING (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND cortex_core.caller_installation_matches(installation_id)
    )
    WITH CHECK (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND cortex_core.caller_installation_matches(installation_id)
    );
CREATE POLICY workers_migrator_all ON cortex_processing.workers
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY processing_outbox_scope_read ON cortex_processing.outbox_events
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY processing_outbox_scope_insert ON cortex_processing.outbox_events
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY processing_outbox_scope_update ON cortex_processing.outbox_events
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY processing_outbox_migrator_all ON cortex_processing.outbox_events
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.provider_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.provider_roles FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_roles_installation_read ON cortex_processing.provider_roles
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY provider_roles_migrator_all ON cortex_processing.provider_roles
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.local_inference_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.local_inference_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY local_inference_read ON cortex_processing.local_inference_policies
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY local_inference_migrator_all ON cortex_processing.local_inference_policies
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_processing.local_model_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_processing.local_model_manifests FORCE ROW LEVEL SECURITY;
CREATE POLICY local_model_manifests_read ON cortex_processing.local_model_manifests
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY local_model_manifests_migrator_all ON cortex_processing.local_model_manifests
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- Grants: the least DML each path needs. Nothing is granted to PUBLIC, no
-- default privileges are set, and no role receives DELETE on a ledger.
GRANT USAGE ON SCHEMA cortex_processing TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_processing.outbox_events TO cortex_v2_app;
GRANT SELECT ON cortex_processing.processing_profiles TO cortex_v2_app;
GRANT SELECT ON cortex_processing.embedding_spaces TO cortex_v2_app;
GRANT SELECT, UPDATE ON cortex_processing.index_generations TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_processing.jobs TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_processing.attempts TO cortex_v2_app;
GRANT SELECT ON cortex_processing.budget_defaults, cortex_processing.budget_policies
    TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_processing.budget_reservations TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_processing.chunk_revisions TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_processing.chunk_vectors TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_processing.distillations TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_processing.quarantine_ledger TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_processing.workers TO cortex_v2_app;
GRANT SELECT ON cortex_processing.provider_roles,
    cortex_processing.local_inference_policies,
    cortex_processing.local_model_manifests TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_processing.profile_shape_violation(jsonb, jsonb, jsonb, jsonb, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_processing_profile() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.retire_processing_profile() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.reject_processing_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.job_intent_violation(text, jsonb, uuid, uuid, uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_job_intent() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_job_transition() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_outbox_insert() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_outbox_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_attempt_transition() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_budget_release() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_quarantine_resolution() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_worker_identity() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_generation_transition() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_space_pointer() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_space_generation_consistency() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.span_violation(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_chunk_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_chunk_vector() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION cortex_processing.budget_capacity(uuid, text) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_processing.budget_interactive_reserve(uuid, text) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_processing.budget_capacity(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.budget_interactive_reserve(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_processing.validate_budget_reservation() FROM PUBLIC;

REVOKE ALL ON FUNCTION cortex_processing.create_embedding_space(uuid, text, text, text, text, integer, text, text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.create_embedding_space(uuid, text, text, text, text, integer, text, text, text, text, jsonb) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_processing.begin_index_generation(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.begin_index_generation(uuid, uuid, uuid) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_processing.activate_index_generation(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.activate_index_generation(uuid, uuid, uuid) TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_processing.active_provider_role(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.active_provider_role(text) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_processing.active_local_inference_policy() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.active_local_inference_policy() TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_processing.vector_candidates(uuid, public.vector, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.vector_candidates(uuid, public.vector, integer) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_processing.space_state(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_processing.space_state(uuid) TO cortex_v2_app;

-- Trigger helpers are invoked while the runtime role inserts, and a plain
-- (non-SECURITY DEFINER) trigger function runs with the calling role's
-- privileges, so the app role needs EXECUTE on the validators it triggers.
-- These are IMMUTABLE/STABLE shape checks with no dynamic SQL and no public
-- EXECUTE; every other function in this schema stays revoked from PUBLIC.
GRANT EXECUTE ON FUNCTION cortex_processing.profile_shape_violation(jsonb, jsonb, jsonb, jsonb, jsonb) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_processing.job_intent_violation(text, jsonb, uuid, uuid, uuid, uuid, text) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_processing.span_violation(jsonb) TO cortex_v2_app;
