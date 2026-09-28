-- Separate converter SQL version; no product migration number is consumed.
DO $guard$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'converter reconciliation requires cortex_v2_migrator';
    END IF;
END
$guard$;

CREATE TABLE cortex_conversion.source_counts (
    run_id uuid NOT NULL REFERENCES cortex_conversion.runs(run_id),
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_scope text NOT NULL,
    source_count bigint NOT NULL CHECK (source_count >= 0),
    source_rowset_sha256 bytea NOT NULL
        CHECK (octet_length(source_rowset_sha256) = 32),
    PRIMARY KEY (run_id, source_schema, source_table, source_scope)
);

CREATE TABLE cortex_conversion.reconciliation (
    run_id uuid NOT NULL,
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_scope text NOT NULL,
    target_relation text NOT NULL,
    source_count bigint NOT NULL CHECK (source_count >= 0),
    target_count bigint NOT NULL CHECK (target_count >= 0),
    quarantined_count bigint NOT NULL CHECK (quarantined_count >= 0),
    skipped_count bigint NOT NULL CHECK (skipped_count >= 0),
    memory_projection_count bigint NOT NULL CHECK (memory_projection_count >= 0),
    verified_body_hash_count bigint NOT NULL
        CHECK (verified_body_hash_count >= 0),
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_schema, source_table, source_scope),
    FOREIGN KEY (run_id, source_schema, source_table, source_scope)
        REFERENCES cortex_conversion.source_counts
            (run_id, source_schema, source_table, source_scope),
    CHECK (source_count = target_count + quarantined_count + skipped_count),
    CHECK (memory_projection_count <= target_count),
    CHECK (verified_body_hash_count = target_count)
);

REVOKE ALL ON ALL TABLES IN SCHEMA cortex_conversion FROM PUBLIC;
