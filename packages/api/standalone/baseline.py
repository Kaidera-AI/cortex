#!/usr/bin/env python3
"""Build an atomic, archive-derived receipt for the dump's proven migration prefix.

Run against the exported BUILD context, never an unfiltered checkout. The pinned
inventory deliberately excludes migrations at and beyond the coverage cutoff,
even when the dump contains some of their objects. Those files belong to the
normal migration engine, including its reviewed postcondition reconciliation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


DEFAULT_MANIFEST = Path(__file__).with_name("baseline.json")
MIGRATIONS_TABLE = "cortex_standalone_schema_baseline_migrations"
RECEIPT_TABLE = "cortex_standalone_schema_baseline_receipt"
FIELDS = {
    "schema_version", "source_revision", "cutoff_exclusive", "migration_count",
    "inventory_sha256", "schema_source_sha256",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+\.sql$")


def inventory_sha256(rows: list[tuple[str, str]]) -> str:
    canonical = "".join(f"{name}\t{digest}\n" for name, digest in sorted(rows))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != FIELDS:
        raise RuntimeError("invalid standalone baseline manifest fields")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise RuntimeError("unsupported standalone baseline manifest version")
    if type(manifest["migration_count"]) is not int or manifest["migration_count"] <= 0:
        raise RuntimeError("invalid standalone baseline migration count")
    for name, width in (("source_revision", 40), ("inventory_sha256", 64),
                        ("schema_source_sha256", 64)):
        if not re.fullmatch(rf"[0-9a-f]{{{width}}}", str(manifest[name])):
            raise RuntimeError(f"invalid standalone baseline {name}")
    if not SAFE_ID.fullmatch(str(manifest["cutoff_exclusive"])):
        raise RuntimeError("invalid standalone baseline cutoff")
    return manifest


def validate_inventory(manifest: dict, sources: dict[str, str]) -> list[tuple[str, str]]:
    for name, checksum in sources.items():
        if not SAFE_ID.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise RuntimeError("unsafe standalone migration inventory")
    selected = sorted((name, digest) for name, digest in sources.items()
                      if name < manifest["cutoff_exclusive"])
    if (len(selected) != manifest["migration_count"]
            or inventory_sha256(selected) != manifest["inventory_sha256"]):
        raise RuntimeError("standalone baseline differs from the pinned build-archive inventory")
    return selected


def render_sql(schema: Path, migrations: Path, manifest: dict) -> str:
    if hashlib.sha256(schema.read_bytes()).hexdigest() != manifest["schema_source_sha256"]:
        raise RuntimeError("standalone bootstrap schema differs from the pinned source bytes")
    if not migrations.is_dir():
        raise RuntimeError("standalone build migration directory is unavailable")
    rows = validate_inventory(manifest, {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in migrations.glob("*.sql")
    })
    values = ",\n".join(f"('{name}', '{digest}')" for name, digest in rows)
    # All interpolated values are validated by load_manifest/validate_inventory.
    receipt_values = (f"TRUE, {manifest['schema_version']}, '{manifest['source_revision']}', "
                      f"'{manifest['cutoff_exclusive']}', {manifest['migration_count']}, "
                      f"'{manifest['inventory_sha256']}', '{manifest['schema_source_sha256']}'")
    return f"""-- cortex-schema-sha256: {manifest['schema_source_sha256']}
BEGIN;
CREATE TEMP TABLE cortex_expected_baseline (
    migration_id TEXT PRIMARY KEY, checksum_sha256 TEXT NOT NULL
) ON COMMIT DROP;
INSERT INTO cortex_expected_baseline VALUES
{values};
CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
    migration_id TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{{64}}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version INTEGER NOT NULL,
    source_revision TEXT NOT NULL,
    cutoff_exclusive TEXT NOT NULL,
    migration_count INTEGER NOT NULL CHECK (migration_count > 0),
    inventory_sha256 TEXT NOT NULL,
    schema_source_sha256 TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $baseline$
BEGIN
    IF EXISTS (SELECT 1 FROM {RECEIPT_TABLE}) THEN
        IF NOT EXISTS (
            SELECT 1 FROM {RECEIPT_TABLE}
            WHERE singleton IS TRUE
              AND schema_version = {manifest['schema_version']}
              AND source_revision = '{manifest['source_revision']}'
              AND cutoff_exclusive = '{manifest['cutoff_exclusive']}'
              AND migration_count = {manifest['migration_count']}
              AND inventory_sha256 = '{manifest['inventory_sha256']}'
              AND schema_source_sha256 = '{manifest['schema_source_sha256']}'
        ) OR EXISTS (
            (SELECT migration_id, checksum_sha256 FROM cortex_expected_baseline
             EXCEPT SELECT migration_id, checksum_sha256 FROM {MIGRATIONS_TABLE})
            UNION ALL
            (SELECT migration_id, checksum_sha256 FROM {MIGRATIONS_TABLE}
             EXCEPT SELECT migration_id, checksum_sha256 FROM cortex_expected_baseline)
        ) THEN
            RAISE EXCEPTION 'standalone schema baseline receipt is incomplete or different';
        END IF;
    ELSE
        IF EXISTS (SELECT 1 FROM {MIGRATIONS_TABLE}) THEN
            RAISE EXCEPTION 'standalone schema baseline has unreceipted rows';
        END IF;
        INSERT INTO {MIGRATIONS_TABLE} (migration_id, checksum_sha256)
            SELECT migration_id, checksum_sha256 FROM cortex_expected_baseline
            ORDER BY migration_id;
        INSERT INTO {RECEIPT_TABLE}
            (singleton, schema_version, source_revision, cutoff_exclusive,
             migration_count, inventory_sha256, schema_source_sha256)
            VALUES ({receipt_values});
    END IF;
END
$baseline$;
ALTER TABLE {MIGRATIONS_TABLE} OWNER TO postgres;
ALTER TABLE {RECEIPT_TABLE} OWNER TO postgres;
REVOKE ALL ON {MIGRATIONS_TABLE}, {RECEIPT_TABLE} FROM PUBLIC;
REVOKE ALL ON {MIGRATIONS_TABLE}, {RECEIPT_TABLE} FROM cortex_app;
COMMIT;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-sql", action="store_true", required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    print(render_sql(args.schema, args.migrations, load_manifest(args.manifest)), end="")


if __name__ == "__main__":
    main()
