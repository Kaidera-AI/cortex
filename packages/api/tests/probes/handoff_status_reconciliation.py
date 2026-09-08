#!/usr/bin/env python3
"""Finite, synthetic PostgreSQL 18 status-contract probe; never uses a DSN.

Run directly, not through pytest. Missing dependencies are failures, never skips.
The optional migration must have the exact allocated basename. Without it the
fixture remains restrictive, and the required-eight-state assertion must fail.
This proves SQL behavior only, NOT production migration-engine ledger atomicity.
Every run creates one new networkless container. No existing container is exec'd,
stopped or removed. Podman's --rm/--timeout own the disposable container lifetime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time

PODMAN = "/opt/podman/bin/podman"
IMAGE = "docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d"
IMAGE_ID = "0d91144b1188e393f324b5fa86291cd9711eee2022a77f58c5ed8a04f6215390"
MIGRATION_NAME = "2026-09-07-01-handoff-status-reconciliation.sql"
HISTORICAL_ID = "2026-07-26-handoff-completion-handback.sql"
HISTORICAL_SHA = "a7ec13ada8554a356f7ab8a6feba5467ef3192069c5f757eeae828cc55a7069b"
SIX_STATES = "'pending','claimed','completed','archived','abandoned','failed'"
EIGHT_STATES = SIX_STATES + ",'returned','released'"

FIXTURE = f"""
CREATE TABLE public.handoffs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project text NOT NULL,
 status text NOT NULL, summary text NOT NULL, invalidated_at timestamptz,
 kind text NOT NULL DEFAULT 'task', reply_to_handoff_id uuid,
 returned_at timestamptz, completion_report jsonb NOT NULL DEFAULT '{{}}',
 CONSTRAINT handoffs_status_check CHECK (status IN ({SIX_STATES})),
 CONSTRAINT handoffs_kind_check CHECK (kind IN ('task','completion_handback')),
 CONSTRAINT handoffs_reply_to_handoff_id_fkey
   FOREIGN KEY (reply_to_handoff_id) REFERENCES public.handoffs(id)
);
CREATE TABLE public.archive_handoffs (LIKE public.handoffs INCLUDING DEFAULTS);
CREATE INDEX idx_handoffs_reply_to_handoff_id ON public.handoffs(reply_to_handoff_id);
CREATE UNIQUE INDEX idx_handoffs_completion_handback_active
 ON public.handoffs(project,reply_to_handoff_id)
 WHERE kind='completion_handback' AND invalidated_at IS NULL
 AND status IN ('pending','claimed');
CREATE TABLE public.cortex_schema_migrations (
 migration_id text PRIMARY KEY, checksum_sha256 text NOT NULL,
 applied_by text NOT NULL, applied_at timestamptz NOT NULL,
 statement_status text NOT NULL
);
INSERT INTO public.cortex_schema_migrations VALUES
 ('{HISTORICAL_ID}','{HISTORICAL_SHA}','kos-baked-baseline',
  '2026-09-01T20:18:56.258834Z','BASELINE ADOPTED');
INSERT INTO public.handoffs(project,status,summary)
 SELECT 'synthetic-status-probe',s,'retained synthetic history'
 FROM unnest(ARRAY[{SIX_STATES}]) AS s;
INSERT INTO public.handoffs(project,status,summary,kind,reply_to_handoff_id)
 SELECT project,'pending','retained synthetic child','completion_handback',id
 FROM public.handoffs WHERE status='claimed';
CREATE TABLE proof_history AS SELECT jsonb_agg(to_jsonb(h) ORDER BY id) AS data
 FROM public.handoffs h;
CREATE TABLE proof_receipt AS SELECT to_jsonb(m) AS data
 FROM public.cortex_schema_migrations m;
CREATE TABLE proof_catalog AS
 SELECT 'constraint:'||conname AS name,pg_get_constraintdef(oid) AS definition
 FROM pg_constraint WHERE conrelid='public.handoffs'::regclass
 AND conname!='handoffs_status_check'
 UNION ALL SELECT 'index:'||indexname,indexdef FROM pg_indexes
 WHERE schemaname='public' AND tablename='handoffs';
"""

RESTRICTIVE = """
DO $$ DECLARE s text; c text; BEGIN
 FOREACH s IN ARRAY ARRAY['returned','released'] LOOP
  BEGIN
   UPDATE public.handoffs SET status=s WHERE status='claimed' AND kind='task';
   RAISE EXCEPTION 'restrictive fixture unexpectedly accepted %',s;
  EXCEPTION WHEN check_violation THEN
   GET STACKED DIAGNOSTICS c=CONSTRAINT_NAME;
   IF c!='handoffs_status_check' THEN RAISE; END IF;
  END;
 END LOOP;
END $$;
SELECT 'RESTRICTIVE_FIXTURE_CONFIRMED';
"""

VERIFY = f"""
DO $$ DECLARE s text; test_id uuid; c text; BEGIN
 SELECT id INTO STRICT test_id FROM public.handoffs WHERE status='claimed' AND kind='task';
 FOREACH s IN ARRAY ARRAY[{EIGHT_STATES}] LOOP
  BEGIN
   UPDATE public.handoffs SET status=s WHERE id=test_id;
   UPDATE public.handoffs SET status='claimed' WHERE id=test_id;
  EXCEPTION WHEN check_violation THEN
   RAISE EXCEPTION 'REGRESSION: required lifecycle state rejected: %',s;
  END;
 END LOOP;
 FOREACH s IN ARRAY ARRAY['invalid','','RETURNED',' released '] LOOP
  BEGIN
   INSERT INTO public.handoffs(project,status,summary)
    VALUES('synthetic-status-probe',s,'must reject');
   RAISE EXCEPTION 'REGRESSION: invalid lifecycle state accepted: %',s;
  EXCEPTION WHEN check_violation THEN
   GET STACKED DIAGNOSTICS c=CONSTRAINT_NAME;
   IF c!='handoffs_status_check' THEN RAISE; END IF;
  END;
 END LOOP;
 IF (SELECT jsonb_agg(to_jsonb(h) ORDER BY id) FROM public.handoffs h)
   IS DISTINCT FROM (SELECT data FROM proof_history) THEN
  RAISE EXCEPTION 'REGRESSION: existing handoff history changed';
 END IF;
 IF (SELECT jsonb_agg(to_jsonb(m)) FROM public.cortex_schema_migrations m)
   IS DISTINCT FROM (SELECT jsonb_agg(data) FROM proof_receipt) THEN
  RAISE EXCEPTION 'REGRESSION: historical migration receipt changed';
 END IF;
 IF EXISTS (
  WITH observed_catalog AS (
   SELECT 'constraint:'||conname AS name,pg_get_constraintdef(oid) AS definition
   FROM pg_constraint WHERE conrelid='public.handoffs'::regclass
   AND conname!='handoffs_status_check'
   UNION ALL SELECT 'index:'||indexname,indexdef FROM pg_indexes
   WHERE schemaname='public' AND tablename='handoffs'
  )
  (SELECT * FROM observed_catalog EXCEPT SELECT * FROM proof_catalog)
  UNION ALL (SELECT * FROM proof_catalog EXCEPT SELECT * FROM observed_catalog)
 ) THEN RAISE EXCEPTION 'REGRESSION: retained constraints/indexes changed'; END IF;
END $$;
SELECT 'EIGHT_STATES_INVALID_REJECTION_HISTORY_CATALOG_PASS';
"""


def psql(sql: str) -> str:
    delimiter = "CORTEX_SQL_" + hashlib.sha256(sql.encode()).hexdigest()
    if delimiter in sql:
        raise ValueError("SQL delimiter collision")
    return f"psql -X -qAt -v ON_ERROR_STOP=1 -h /tmp/status-socket -U postgres -d postgres 2>&1 <<'{delimiter}'\n{sql}\n{delimiter}\n"


def program(migration: str) -> str:
    apply = psql("BEGIN;\n" + migration + "\nCOMMIT;")
    reset = f"""
ALTER TABLE public.handoffs DROP CONSTRAINT handoffs_status_check;
ALTER TABLE public.handoffs ADD CONSTRAINT handoffs_status_check
 CHECK(status IN ({SIX_STATES}));
"""
    return r"""set -eu
umask 077
export PATH=/usr/lib/postgresql/18/bin:/usr/bin:/bin
export LC_ALL=C
export PGDATA=/var/lib/postgresql/status-probe
export PGOPTIONS='-c statement_timeout=5000 -c lock_timeout=2000'
cleanup() {
 result=$?
 trap - EXIT INT TERM
 if [ -s "$PGDATA/postmaster.pid" ]; then
  pg_ctl -D "$PGDATA" -m fast -t 5 -w stop || result=2
 fi
 printf 'SERVER_CLEANUP_EXIT=%s\n' "$result"
 exit "$result"
}
trap cleanup EXIT
trap 'exit 143' INT TERM
test "$(id -u)" = 999
test "$(id -g)" = 999
mkdir /tmp/status-socket
initdb -D "$PGDATA" --no-locale --encoding=UTF8 --auth-local=trust --auth-host=reject >/dev/null
pg_ctl -D "$PGDATA" -l /tmp/status-postgres.log -t 20 -w start \
 -o "-c listen_addresses='' -c unix_socket_directories=/tmp/status-socket -c unix_socket_permissions=0700 -c shared_buffers=16MB -c max_connections=8 -c fsync=on"
psql -X -qAt -v ON_ERROR_STOP=1 -h /tmp/status-socket -U postgres -d postgres \
 -c "SELECT 'SERVER_VERSION='||current_setting('server_version'); DO \$\$ BEGIN IF current_setting('server_version_num')::integer NOT BETWEEN 180000 AND 189999 THEN RAISE EXCEPTION 'PostgreSQL18 required'; END IF; END \$\$;"
""" + psql(FIXTURE + RESTRICTIVE) + apply + psql(VERIFY) + \
        apply + psql(VERIFY + "SELECT 'RERUN_IDEMPOTENCE_PASS';") + \
        psql("BEGIN;\n" + reset + "\nCOMMIT;\n" + RESTRICTIVE) + "if fault_output=$(\n" + psql(
            "\\set VERBOSITY sqlstate\nBEGIN;\n" + migration + "\nSELECT 'ROLLBACK_FAULT_ARMED';\nSELECT 1/0;\nCOMMIT;"
        ) + ")\nthen\n printf 'ERROR: expected transaction fault did not occur\\n'\n exit 2\nfi\n" + \
        "printf '%s\\n' \"$fault_output\"\ncase \"$fault_output\" in\n" + \
        " *'ROLLBACK_FAULT_ARMED'*'ERROR:  22012'*) ;;\n *) printf 'ERROR: unrelated rollback failure\\n'; exit 2;;\nesac\n" + \
        psql(RESTRICTIVE + "SELECT 'FAILED_TRANSACTION_ROLLBACK_PASS';") + \
        apply + psql(VERIFY + "SELECT 'SCHEMA_PROBE_PASS';")


def bounded_run(command: list[str], payload: bytes = b"", timeout: int = 15) -> tuple[int, bytes]:
    """One owned client, bounded stdin/output/wall time; never touches other PIDs."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())}
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdin.fileno(), False)
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            if payload:
                selector.register(child.stdin, selectors.EVENT_WRITE)
            else:
                child.stdin.close()
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("finite client deadline exceeded; disposable engine timeout remains active")
                for key, _ in selector.select(max(0.0, min(0.2, deadline-time.monotonic()))):
                    if key.fileobj is child.stdin:
                        try:
                            payload = payload[os.write(child.stdin.fileno(), payload[:4096]):]
                        except BrokenPipeError:
                            payload = b""
                        if not payload:
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), 4096)
                        if not chunk:
                            selector.unregister(child.stdout)
                        elif len(output) + len(chunk) > 262144:
                            raise ValueError("finite client output bound exceeded")
                        else:
                            output.extend(chunk)
            return child.wait(timeout=max(0.1, deadline-time.monotonic())), bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, help="optional reviewed allocated migration; no arbitrary DSN")
    args = parser.parse_args()
    migration_path = args.migration or Path(__file__).resolve().parents[3] / "data/migrations" / MIGRATION_NAME
    migration = "SELECT 'MIGRATION_ABSENT_BASELINE';"
    migration_sha = "absent"
    if migration_path.exists():
        info = migration_path.lstat()
        if migration_path.name != MIGRATION_NAME or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise ValueError("migration must be the bounded regular allocated SQL file")
        raw = migration_path.read_bytes()
        migration = raw.decode("utf-8")
        if any(line.lstrip().startswith("\\") for line in migration.splitlines()):
            raise ValueError("psql metacommands are not migration SQL")
        migration_sha = hashlib.sha256(raw).hexdigest()
    elif args.migration:
        raise ValueError("explicit migration prerequisite is absent")
    rc, output = bounded_run([PODMAN, "image", "inspect", IMAGE])
    if rc:
        raise RuntimeError("exact preinstalled PostgreSQL image unavailable; no pull attempted")
    rows = json.loads(output)
    if len(rows) != 1 or rows[0].get("Id") != IMAGE_ID or rows[0].get("Os") != "linux" or rows[0].get("Architecture") != "arm64" or "PG_MAJOR=18" not in rows[0].get("Config", {}).get("Env", []):
        raise ValueError("local image identity/platform/PostgreSQL-major mismatch")
    nonce = secrets.token_hex(12)
    name = "Cortex_kai_test_status_" + nonce
    command = [PODMAN, "run", "--rm", "--interactive", "--pull=never", "--name", name,
               "--label", "io.kaidera.cortex.status-probe=" + nonce,
               "--network=none", "--user=999:999", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--read-only", "--read-only-tmpfs=false",
               "--image-volume=ignore", "--tmpfs", "/var/lib/postgresql:rw,size=268435456,mode=1777",
               "--tmpfs", "/tmp:rw,size=16777216,mode=1777",
               "--shm-size=16m", "--memory=256m", "--memory-swap=256m", "--cpus=1",
               "--pids-limit=64", "--timeout=105", "--stop-timeout=5", "--log-driver=none",
               "--entrypoint=/usr/bin/timeout", IMAGE_ID, "--signal=TERM", "--kill-after=5s", "90s", "/bin/sh", "-s"]
    print(json.dumps({"container_name": name, "image_id": IMAGE_ID, "image_digest": IMAGE,
                      "migration_sha256": migration_sha, "command": command}), flush=True)
    rc, output = bounded_run(command, program(migration).encode(), timeout=120)
    print(output.decode("utf-8", errors="strict"), end="")
    print("CONTAINER_OUTPUT_SHA256=" + hashlib.sha256(output).hexdigest())
    absent, diagnostic = bounded_run([PODMAN, "container", "exists", name])
    if absent != 1 or diagnostic:
        raise RuntimeError("disposable container absence unproven; no guessed cleanup attempted")
    print("OWNED_CONTAINER_ABSENT=true")
    print("PROBE_EXIT=" + str(rc))
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        print("PROBE_ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
