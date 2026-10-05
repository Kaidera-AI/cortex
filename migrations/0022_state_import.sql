-- R148: private, append-only conversion evidence. No application access or authority issue.
DO $guard$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'state import migration requires cortex_v2_migrator';
    END IF;
END
$guard$;

CREATE TABLE cortex_core.import_runs (
    run_id uuid NOT NULL,
    event_seq bigint NOT NULL CHECK (event_seq >= 0),
    binding_seq bigint NOT NULL DEFAULT 0 CHECK (binding_seq = 0),
    event_kind text NOT NULL CHECK (event_kind IN ('binding', 'batch', 'complete')),
    checkpoint bigint NOT NULL CHECK (checkpoint >= 0),
    target_installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, event_seq),
    FOREIGN KEY (run_id, binding_seq) REFERENCES cortex_core.import_runs(run_id, event_seq),
    CHECK ((event_seq = 0) = (event_kind = 'binding')),
    CHECK (event_kind <> 'binding' OR checkpoint = 0)
);
CREATE UNIQUE INDEX import_runs_one_completion ON cortex_core.import_runs(run_id)
    WHERE event_kind = 'complete';

CREATE TABLE cortex_core.import_rows (
    run_id uuid NOT NULL,
    binding_seq bigint NOT NULL DEFAULT 0 CHECK (binding_seq = 0),
    ordinal bigint NOT NULL CHECK (ordinal >= 0),
    family text NOT NULL CHECK (family = 'projects'),
    source_reference text NOT NULL CHECK (length(source_reference) > 0),
    original_bytes bytea NOT NULL CHECK (octet_length(original_bytes) > 0),
    source_sha256 bytea NOT NULL CHECK (octet_length(source_sha256) = 32 AND source_sha256 = sha256(original_bytes)),
    outcome text NOT NULL CHECK (outcome IN ('migrated', 'quarantined')),
    reason text,
    scope_id uuid REFERENCES cortex_core.scopes(scope_id),
    target_references jsonb NOT NULL CHECK (jsonb_typeof(target_references) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, ordinal),
    UNIQUE (run_id, source_reference),
    FOREIGN KEY (run_id, binding_seq) REFERENCES cortex_core.import_runs(run_id, event_seq),
    CHECK ((outcome = 'migrated' AND reason IS NULL AND scope_id IS NOT NULL
            AND jsonb_array_length(target_references) > 0)
        OR (outcome = 'quarantined' AND reason IS NOT NULL AND length(reason) > 0
            AND scope_id IS NULL AND jsonb_array_length(target_references) = 0))
);

CREATE TABLE cortex_core.import_blob_chunks (
    run_id uuid NOT NULL,
    binding_seq bigint NOT NULL DEFAULT 0 CHECK (binding_seq = 0),
    blob_sha256 bytea NOT NULL CHECK (octet_length(blob_sha256) = 32),
    chunk_index bigint NOT NULL CHECK (chunk_index >= 0),
    chunk_bytes bytea NOT NULL CHECK (octet_length(chunk_bytes) BETWEEN 1 AND 65536),
    chunk_sha256 bytea NOT NULL CHECK (octet_length(chunk_sha256) = 32 AND chunk_sha256 = sha256(chunk_bytes)),
    total_bytes bigint NOT NULL CHECK (total_bytes > 0),
    chunk_count bigint NOT NULL CHECK (chunk_count > 0 AND chunk_index < chunk_count),
    PRIMARY KEY (run_id, blob_sha256, chunk_index),
    FOREIGN KEY (run_id, binding_seq) REFERENCES cortex_core.import_runs(run_id, event_seq)
);

CREATE FUNCTION cortex_core.reject_import_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION 'import ledger is append-only' USING ERRCODE = '23514';
END
$function$;
REVOKE ALL ON FUNCTION cortex_core.reject_import_mutation() FROM PUBLIC, cortex_v2_app;

DO $protect$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['import_runs', 'import_rows', 'import_blob_chunks'] LOOP
        EXECUTE format('ALTER TABLE cortex_core.%I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE cortex_core.%I FORCE ROW LEVEL SECURITY', name);
        EXECUTE format('CREATE POLICY %I ON cortex_core.%I FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true)', name || '_migrator', name);
        EXECUTE format('REVOKE ALL ON cortex_core.%I FROM PUBLIC, cortex_v2_app', name);
        EXECUTE format('GRANT SELECT, INSERT ON cortex_core.%I TO cortex_v2_migrator', name);
        -- Statement triggers also reject mutation of an empty table, including TRUNCATE.
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE OR TRUNCATE ON cortex_core.%I FOR EACH STATEMENT EXECUTE FUNCTION cortex_core.reject_import_mutation()', name || '_immutable', name);
    END LOOP;
END
$protect$;
