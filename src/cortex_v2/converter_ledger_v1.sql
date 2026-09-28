-- Converter-owned schema, intentionally outside the product migration sequence.
DO $guard$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'converter schema requires cortex_v2_migrator';
    END IF;
END
$guard$;

CREATE SCHEMA cortex_conversion AUTHORIZATION cortex_v2_migrator;

CREATE TABLE cortex_conversion.schema_versions (
    version text PRIMARY KEY,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_conversion.runs (
    run_id uuid PRIMARY KEY,
    snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    source_schema_hash text NOT NULL CHECK (source_schema_hash ~ '^[0-9a-f]{64}$'),
    target_schema_hash text NOT NULL CHECK (target_schema_hash ~ '^[0-9a-f]{64}$'),
    converter_revision text NOT NULL CHECK (length(converter_revision) BETWEEN 7 AND 128),
    policy_sha256 text NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL,
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'complete')),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (snapshot_sha256, source_schema_hash, target_schema_hash,
            converter_revision, policy_sha256, installation_id),
    CHECK ((status = 'complete') = (completed_at IS NOT NULL))
);

CREATE TABLE cortex_conversion.outcomes (
    run_id uuid NOT NULL REFERENCES cortex_conversion.runs(run_id),
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_pk text NOT NULL,
    source_hash bytea NOT NULL CHECK (octet_length(source_hash) = 32),
    source_scope text NOT NULL DEFAULT '<unmapped>',
    target_relation text,
    target_scope_id uuid,
    target_id uuid,
    target_revision integer,
    body_sha256 bytea,
    outcome text NOT NULL CHECK (outcome IN ('migrated', 'quarantined', 'skipped')),
    reason text,
    quarantine_id uuid REFERENCES cortex_core.conversion_quarantine(quarantine_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_schema, source_table, source_pk),
    CHECK (source_schema IN ('public', 'cortex', 'cortex_auth')),
    CHECK (octet_length(source_pk) BETWEEN 1 AND 512),
    CHECK (body_sha256 IS NULL OR octet_length(body_sha256) = 32),
    CHECK (
        (outcome = 'migrated' AND reason IS NULL AND target_relation IS NOT NULL
         AND target_scope_id IS NOT NULL AND target_id IS NOT NULL
         AND quarantine_id IS NULL)
        OR (outcome = 'quarantined' AND reason IS NOT NULL
            AND quarantine_id IS NOT NULL AND target_id IS NULL)
        OR (outcome = 'skipped' AND reason IS NOT NULL
            AND quarantine_id IS NULL AND target_id IS NULL)
    )
);

CREATE INDEX conversion_outcomes_class
    ON cortex_conversion.outcomes(run_id, source_schema, source_table, outcome, reason);

REVOKE ALL ON SCHEMA cortex_conversion FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA cortex_conversion FROM PUBLIC;
