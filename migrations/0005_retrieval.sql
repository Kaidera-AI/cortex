DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'retrieval migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

-- ---------------------------------------------------------------------------
-- W3 retrieval plane: source-bound memory graph, commit-bound code graph,
-- trigram projection and cached change assessments. Every relation carries an
-- explicit scope; graph assertions keep normalized evidence edges to canonical
-- content revisions so a retracted or superseded source retracts exactly its
-- own derived evidence (F09). Authored annotations are canonical and are never
-- removed by projection rebuilds.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS cortex_retrieval AUTHORIZATION cortex_v2_migrator;

-- pg_trgm is installed in ``public`` by the bootstrap database owner before
-- migrations run (integrated bootstrap contract: the owner executes
-- CREATE EXTENSION vector/pg_trgm SCHEMA public). The migrator is not the
-- extension owner, so it must never create in, relocate to or otherwise
-- re-schema an owner-installed extension; this migration requires the
-- pinned location and fails closed with an explicit message instead.
DO $trgm$
DECLARE
    trgm_schema text;
BEGIN
    SELECT n.nspname
      INTO trgm_schema
      FROM pg_catalog.pg_extension AS e
      JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_trgm';
    IF trgm_schema IS NULL THEN
        RAISE EXCEPTION
            'pg_trgm must be installed in public by the bootstrap owner before 0005_retrieval.sql';
    END IF;
    IF trgm_schema <> 'public' THEN
        RAISE EXCEPTION
            'pg_trgm is installed in %, but the bootstrap contract pins it to public; the migrator never relocates extensions it does not own',
            trgm_schema;
    END IF;
END
$trgm$;

-- ---------------------------------------------------------------------------
-- Memory graph: entities, extraction runs, assertions and evidence edges.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_retrieval.graph_entities (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    entity_id uuid NOT NULL,
    entity_key text NOT NULL CHECK (length(entity_key) BETWEEN 1 AND 96),
    entity_type text NOT NULL
        CHECK (entity_type IN ('concept', 'module', 'person', 'service')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, entity_id),
    UNIQUE (scope_id, entity_key, entity_type)
);

CREATE TABLE cortex_retrieval.extraction_runs (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    run_id uuid NOT NULL,
    content_id uuid NOT NULL,
    source_revision integer NOT NULL CHECK (source_revision > 0),
    extractor_profile text NOT NULL CHECK (length(extractor_profile) BETWEEN 1 AND 64),
    assertion_count integer NOT NULL CHECK (assertion_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, run_id),
    FOREIGN KEY (scope_id, content_id, source_revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_retrieval.graph_assertions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    assertion_id uuid NOT NULL,
    subject_entity_id uuid NOT NULL,
    relation text NOT NULL CHECK (relation ~ '^[a-z][a-z0-9_]{0,63}$'),
    object_entity_id uuid NOT NULL,
    origin text NOT NULL CHECK (origin IN ('extracted', 'authored')),
    author_principal_id uuid,
    extraction_run_id uuid,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    retracted_at timestamptz,
    retraction_reason text
        CHECK (retraction_reason IS NULL OR retraction_reason IN (
            'source_invalidated', 'source_superseded', 'source_deleted',
            'rebuild', 'command'
        )),
    PRIMARY KEY (scope_id, assertion_id),
    FOREIGN KEY (scope_id, subject_entity_id)
        REFERENCES cortex_retrieval.graph_entities(scope_id, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, object_entity_id)
        REFERENCES cortex_retrieval.graph_entities(scope_id, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, extraction_run_id)
        REFERENCES cortex_retrieval.extraction_runs(scope_id, run_id)
        ON DELETE RESTRICT,
    CHECK (subject_entity_id <> object_entity_id),
    CHECK ((origin = 'authored') = (author_principal_id IS NOT NULL)),
    CHECK ((origin = 'extracted') = (extraction_run_id IS NOT NULL)),
    CHECK (
        (status = 'retracted')
        = (retracted_at IS NOT NULL AND retraction_reason IS NOT NULL)
    )
);

-- One active assertion per subject/relation/object triple: extraction dedupes
-- onto the surviving assertion and adds an evidence edge instead of a copy.
CREATE UNIQUE INDEX graph_assertions_unique_active_triple
    ON cortex_retrieval.graph_assertions
        (scope_id, subject_entity_id, relation, object_entity_id)
    WHERE status = 'active';

CREATE TABLE cortex_retrieval.graph_assertion_evidence (
    scope_id uuid NOT NULL,
    assertion_id uuid NOT NULL,
    evidence_seq integer NOT NULL CHECK (evidence_seq > 0),
    content_id uuid NOT NULL,
    source_revision integer NOT NULL CHECK (source_revision > 0),
    span_start integer NOT NULL CHECK (span_start >= 0),
    span_end integer NOT NULL CHECK (span_end > span_start),
    PRIMARY KEY (scope_id, assertion_id, evidence_seq),
    FOREIGN KEY (scope_id, assertion_id)
        REFERENCES cortex_retrieval.graph_assertions(scope_id, assertion_id)
        ON DELETE CASCADE,
    FOREIGN KEY (scope_id, content_id, source_revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT,
    UNIQUE (scope_id, assertion_id, content_id, source_revision, span_start, span_end)
);

CREATE INDEX graph_assertion_evidence_source
    ON cortex_retrieval.graph_assertion_evidence(scope_id, content_id, source_revision);
CREATE INDEX graph_assertions_subject_active
    ON cortex_retrieval.graph_assertions(scope_id, subject_entity_id)
    WHERE status = 'active';
CREATE INDEX graph_assertions_object_active
    ON cortex_retrieval.graph_assertions(scope_id, object_entity_id)
    WHERE status = 'active';

CREATE FUNCTION cortex_retrieval.reject_graph_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'graph identity and provenance rows are append-only'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER graph_entities_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.graph_entities
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_graph_identity_mutation();

CREATE TRIGGER extraction_runs_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.extraction_runs
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_graph_identity_mutation();

-- Retraction is the only legal assertion mutation: active -> retracted, once.
-- Assertions are never deleted; a retracted row is the tombstone that fences
-- stale derived evidence.
CREATE FUNCTION cortex_retrieval.guard_graph_assertion_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'graph assertions retract; they are never deleted'
            USING ERRCODE = '55000';
        RETURN NULL;
    END IF;
    IF NEW.scope_id <> OLD.scope_id
       OR NEW.assertion_id <> OLD.assertion_id
       OR NEW.subject_entity_id <> OLD.subject_entity_id
       OR NEW.relation <> OLD.relation
       OR NEW.object_entity_id <> OLD.object_entity_id
       OR NEW.origin <> OLD.origin
       OR NEW.author_principal_id IS DISTINCT FROM OLD.author_principal_id
       OR NEW.extraction_run_id IS DISTINCT FROM OLD.extraction_run_id
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'graph assertion identity and provenance are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'retracted' THEN
        RAISE EXCEPTION 'retracted graph assertions are terminal tombstones'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status <> 'retracted'
       OR NEW.retracted_at IS NULL
       OR NEW.retraction_reason IS NULL THEN
        RAISE EXCEPTION 'the only assertion transition is active -> retracted with a reason'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER graph_assertions_guard_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.graph_assertions
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.guard_graph_assertion_mutation();

-- ---------------------------------------------------------------------------
-- Code graph: repositories, commit/snapshot-bound generations, symbols, edges,
-- preserved authored annotations and cached change assessments.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_retrieval.code_repositories (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    repository_id uuid NOT NULL,
    repository_key text NOT NULL CHECK (length(repository_key) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, repository_id),
    UNIQUE (scope_id, repository_key)
);

CREATE TRIGGER code_repositories_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.code_repositories
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_graph_identity_mutation();

CREATE TABLE cortex_retrieval.code_generations (
    scope_id uuid NOT NULL,
    repository_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{7,64}$'),
    extractor_profile text NOT NULL CHECK (length(extractor_profile) BETWEEN 1 AND 64),
    language_coverage text[] NOT NULL CHECK (array_length(language_coverage, 1) > 0),
    status text NOT NULL DEFAULT 'building'
        CHECK (status IN ('building', 'published', 'failed', 'retired')),
    file_count integer NOT NULL DEFAULT 0 CHECK (file_count >= 0),
    excluded_files jsonb NOT NULL DEFAULT '[]'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    PRIMARY KEY (scope_id, repository_id, generation),
    FOREIGN KEY (scope_id, repository_id)
        REFERENCES cortex_retrieval.code_repositories(scope_id, repository_id)
        ON DELETE RESTRICT,
    CHECK (status <> 'published' OR published_at IS NOT NULL),
    CHECK (published_at IS NULL OR status IN ('published', 'retired'))
);

CREATE INDEX code_generations_repository_status
    ON cortex_retrieval.code_generations(scope_id, repository_id, status, generation DESC);
CREATE INDEX code_generations_repository_commit
    ON cortex_retrieval.code_generations(scope_id, repository_id, commit_sha);

CREATE TABLE cortex_retrieval.code_symbols (
    scope_id uuid NOT NULL,
    repository_id uuid NOT NULL,
    generation integer NOT NULL,
    symbol_id uuid NOT NULL,
    symbol_key text NOT NULL CHECK (length(symbol_key) BETWEEN 1 AND 512),
    symbol_kind text NOT NULL
        CHECK (symbol_kind IN ('module', 'class', 'function', 'method')),
    file_path text NOT NULL CHECK (length(file_path) BETWEEN 1 AND 512),
    span_start_line integer NOT NULL CHECK (span_start_line > 0),
    span_end_line integer NOT NULL CHECK (span_end_line >= span_start_line),
    parent_symbol_id uuid,
    PRIMARY KEY (scope_id, repository_id, generation, symbol_id),
    FOREIGN KEY (scope_id, repository_id, generation)
        REFERENCES cortex_retrieval.code_generations(scope_id, repository_id, generation)
        ON DELETE CASCADE,
    FOREIGN KEY (scope_id, repository_id, generation, parent_symbol_id)
        REFERENCES cortex_retrieval.code_symbols(scope_id, repository_id, generation, symbol_id)
        ON DELETE RESTRICT,
    UNIQUE (scope_id, repository_id, generation, symbol_key)
);

CREATE INDEX code_symbols_file
    ON cortex_retrieval.code_symbols(scope_id, repository_id, generation, file_path);

CREATE TABLE cortex_retrieval.code_edges (
    scope_id uuid NOT NULL,
    repository_id uuid NOT NULL,
    generation integer NOT NULL,
    edge_id uuid NOT NULL,
    caller_symbol_id uuid NOT NULL,
    edge_kind text NOT NULL CHECK (edge_kind IN ('calls', 'imports', 'contains')),
    callee_key text NOT NULL CHECK (length(callee_key) BETWEEN 1 AND 512),
    callee_symbol_id uuid,
    source_line integer NOT NULL CHECK (source_line > 0),
    PRIMARY KEY (scope_id, repository_id, generation, edge_id),
    FOREIGN KEY (scope_id, repository_id, generation)
        REFERENCES cortex_retrieval.code_generations(scope_id, repository_id, generation)
        ON DELETE CASCADE,
    FOREIGN KEY (scope_id, repository_id, generation, caller_symbol_id)
        REFERENCES cortex_retrieval.code_symbols(scope_id, repository_id, generation, symbol_id)
        ON DELETE CASCADE,
    FOREIGN KEY (scope_id, repository_id, generation, callee_symbol_id)
        REFERENCES cortex_retrieval.code_symbols(scope_id, repository_id, generation, symbol_id)
        ON DELETE CASCADE
);

CREATE INDEX code_edges_callee
    ON cortex_retrieval.code_edges(scope_id, repository_id, generation, callee_symbol_id);
CREATE INDEX code_edges_caller
    ON cortex_retrieval.code_edges(scope_id, repository_id, generation, caller_symbol_id);
CREATE UNIQUE INDEX code_edges_unique
    ON cortex_retrieval.code_edges
        (scope_id, repository_id, generation, caller_symbol_id, edge_kind,
         callee_key, source_line);

CREATE FUNCTION cortex_retrieval.reject_code_symbol_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'code graph rows are immutable within a generation'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER code_symbols_reject_update
    BEFORE UPDATE ON cortex_retrieval.code_symbols
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_code_symbol_mutation();

CREATE TRIGGER code_edges_reject_update
    BEFORE UPDATE ON cortex_retrieval.code_edges
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_code_symbol_mutation();

-- Generation lifecycle: building -> published|failed, published -> retired.
-- Only failed or retired generations may be pruned; a published generation is
-- never silently removed while it is the coverage basis for queries.
CREATE FUNCTION cortex_retrieval.guard_code_generation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status NOT IN ('failed', 'retired') THEN
            RAISE EXCEPTION 'only failed or retired code generations may be pruned'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.scope_id <> OLD.scope_id
       OR NEW.repository_id <> OLD.repository_id
       OR NEW.generation <> OLD.generation
       OR NEW.commit_sha <> OLD.commit_sha
       OR NEW.extractor_profile <> OLD.extractor_profile
       OR NEW.language_coverage <> OLD.language_coverage
       OR NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION 'code generation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'building'
       AND NOT (
           (NEW.status = 'published' AND NEW.published_at IS NOT NULL)
           OR (NEW.status = 'failed' AND NEW.published_at IS NULL)
       ) THEN
        RAISE EXCEPTION 'building generations finalize to published or failed only'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'published'
       AND NOT (NEW.status = 'retired' AND NEW.published_at = OLD.published_at) THEN
        RAISE EXCEPTION 'published generations may only be retired'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status IN ('failed', 'retired') THEN
        RAISE EXCEPTION 'finalized code generations are terminal'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER code_generations_guard_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.code_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.guard_code_generation_mutation();

-- Authored annotations are canonical human facts about symbols; they are not
-- generation-bound and survive index rebuilds unchanged.
CREATE TABLE cortex_retrieval.code_annotations (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    annotation_id uuid NOT NULL,
    repository_id uuid NOT NULL,
    symbol_key text NOT NULL CHECK (length(symbol_key) BETWEEN 1 AND 512),
    annotation text NOT NULL CHECK (length(annotation) BETWEEN 1 AND 2048),
    author_principal_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, annotation_id),
    FOREIGN KEY (scope_id, repository_id)
        REFERENCES cortex_retrieval.code_repositories(scope_id, repository_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (author_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX code_annotations_symbol
    ON cortex_retrieval.code_annotations(scope_id, repository_id, symbol_key);

CREATE FUNCTION cortex_retrieval.reject_code_annotation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'authored code annotations are canonical and append-only'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER code_annotations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_retrieval.code_annotations
    FOR EACH ROW EXECUTE FUNCTION cortex_retrieval.reject_code_annotation_mutation();

-- Cached change assessments: deduplicate repeated requests for the exact same
-- change digest; an updated diff produces a new digest and a new assessment.
CREATE TABLE cortex_retrieval.code_assessments (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    assessment_id uuid NOT NULL,
    repository_id uuid NOT NULL,
    change_digest bytea NOT NULL CHECK (octet_length(change_digest) = 32),
    generation integer,
    graph_status text NOT NULL
        CHECK (graph_status IN ('fresh', 'stale', 'partial', 'unavailable')),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, assessment_id),
    UNIQUE (scope_id, change_digest),
    FOREIGN KEY (scope_id, repository_id)
        REFERENCES cortex_retrieval.code_repositories(scope_id, repository_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, repository_id, generation)
        REFERENCES cortex_retrieval.code_generations(scope_id, repository_id, generation)
        ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Trigram projection over the latest current canonical content revisions.
-- A replaceable projection: rebuild deletes and repopulates it from the
-- canonical source, and search reports its coverage explicitly.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_retrieval.trigram_documents (
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    source_text text NOT NULL,
    PRIMARY KEY (scope_id, content_id),
    FOREIGN KEY (scope_id, content_id, revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT
);

CREATE INDEX trigram_documents_trgm_gin
    ON cortex_retrieval.trigram_documents
    USING gin (source_text public.gin_trgm_ops);

-- Ordinary RLS-respecting function (not SECURITY DEFINER): the caller's scope
-- policies filter both the canonical read side and the projection write side.
CREATE FUNCTION cortex_retrieval.rebuild_trigram_projection(p_scope_id uuid)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_retrieval, cortex_core, pg_temp
AS $function$
DECLARE
    rebuilt integer;
BEGIN
    DELETE FROM cortex_retrieval.trigram_documents
     WHERE scope_id = p_scope_id;
    INSERT INTO cortex_retrieval.trigram_documents
        (scope_id, content_id, revision, source_text)
    SELECT r.scope_id, r.content_id, r.revision, r.body_text
      FROM cortex_core.content_revisions AS r
     WHERE r.scope_id = p_scope_id
       AND r.revision = (
           SELECT max(latest.revision)
             FROM cortex_core.content_revisions AS latest
            WHERE latest.scope_id = r.scope_id
              AND latest.content_id = r.content_id
       )
       AND cortex_core.content_current_status(r.scope_id, r.content_id) = 'current';
    GET DIAGNOSTICS rebuilt = ROW_COUNT;
    RETURN rebuilt;
END
$function$;

-- ---------------------------------------------------------------------------
-- Row level security: forced for every role including the owner; the same
-- scope-policy helpers from cortex_core decide visibility. Missing context
-- sees nothing; write and lifecycle mutations additionally require the
-- authorized write scope on the old and the new row.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_retrieval.graph_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.graph_entities FORCE ROW LEVEL SECURITY;
CREATE POLICY graph_entities_scope_read ON cortex_retrieval.graph_entities
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY graph_entities_scope_insert ON cortex_retrieval.graph_entities
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY graph_entities_migrator_all ON cortex_retrieval.graph_entities
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.extraction_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.extraction_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY extraction_runs_scope_read ON cortex_retrieval.extraction_runs
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY extraction_runs_scope_insert ON cortex_retrieval.extraction_runs
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY extraction_runs_migrator_all ON cortex_retrieval.extraction_runs
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.graph_assertions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.graph_assertions FORCE ROW LEVEL SECURITY;
CREATE POLICY graph_assertions_scope_read ON cortex_retrieval.graph_assertions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY graph_assertions_scope_insert ON cortex_retrieval.graph_assertions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY graph_assertions_scope_update ON cortex_retrieval.graph_assertions
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY graph_assertions_migrator_all ON cortex_retrieval.graph_assertions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.graph_assertion_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.graph_assertion_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY graph_evidence_scope_read ON cortex_retrieval.graph_assertion_evidence
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY graph_evidence_scope_insert ON cortex_retrieval.graph_assertion_evidence
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY graph_evidence_scope_delete ON cortex_retrieval.graph_assertion_evidence
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY graph_evidence_migrator_all ON cortex_retrieval.graph_assertion_evidence
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_repositories FORCE ROW LEVEL SECURITY;
CREATE POLICY code_repositories_scope_read ON cortex_retrieval.code_repositories
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_repositories_scope_insert ON cortex_retrieval.code_repositories
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_repositories_migrator_all ON cortex_retrieval.code_repositories
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY code_generations_scope_read ON cortex_retrieval.code_generations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_generations_scope_insert ON cortex_retrieval.code_generations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_generations_scope_update ON cortex_retrieval.code_generations
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_generations_scope_delete ON cortex_retrieval.code_generations
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_generations_migrator_all ON cortex_retrieval.code_generations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_symbols ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_symbols FORCE ROW LEVEL SECURITY;
CREATE POLICY code_symbols_scope_read ON cortex_retrieval.code_symbols
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_symbols_scope_insert ON cortex_retrieval.code_symbols
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_symbols_migrator_all ON cortex_retrieval.code_symbols
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY code_edges_scope_read ON cortex_retrieval.code_edges
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_edges_scope_insert ON cortex_retrieval.code_edges
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_edges_migrator_all ON cortex_retrieval.code_edges
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_annotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_annotations FORCE ROW LEVEL SECURITY;
CREATE POLICY code_annotations_scope_read ON cortex_retrieval.code_annotations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_annotations_scope_insert ON cortex_retrieval.code_annotations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND author_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY code_annotations_migrator_all ON cortex_retrieval.code_annotations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.code_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.code_assessments FORCE ROW LEVEL SECURITY;
CREATE POLICY code_assessments_scope_read ON cortex_retrieval.code_assessments
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY code_assessments_scope_insert ON cortex_retrieval.code_assessments
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_assessments_scope_delete ON cortex_retrieval.code_assessments
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY code_assessments_migrator_all ON cortex_retrieval.code_assessments
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_retrieval.trigram_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_retrieval.trigram_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY trigram_documents_scope_read ON cortex_retrieval.trigram_documents
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY trigram_documents_scope_insert ON cortex_retrieval.trigram_documents
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY trigram_documents_scope_delete ON cortex_retrieval.trigram_documents
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY trigram_documents_migrator_all ON cortex_retrieval.trigram_documents
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- Grants: least DML required per projection lifecycle. No default privileges.

GRANT USAGE ON SCHEMA cortex_retrieval TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.graph_entities TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.extraction_runs TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_retrieval.graph_assertions TO cortex_v2_app;
GRANT SELECT, INSERT, DELETE ON cortex_retrieval.graph_assertion_evidence
    TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.code_repositories TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON cortex_retrieval.code_generations
    TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.code_symbols TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.code_edges TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_retrieval.code_annotations TO cortex_v2_app;
GRANT SELECT, INSERT, DELETE ON cortex_retrieval.code_assessments TO cortex_v2_app;
GRANT SELECT, INSERT, DELETE ON cortex_retrieval.trigram_documents TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_retrieval.rebuild_trigram_projection(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_retrieval.rebuild_trigram_projection(uuid)
    TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_retrieval.reject_graph_identity_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_retrieval.guard_graph_assertion_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_retrieval.reject_code_symbol_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_retrieval.guard_code_generation_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_retrieval.reject_code_annotation_mutation() FROM PUBLIC;
