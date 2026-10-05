"""Owned, socket-only PostgreSQL fixture for the admitted R148 minimal slice."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLATION = uuid.UUID("14800000-0000-4000-8000-000000000001")
SOURCE_DDL = """
CREATE TABLE public.cortex_projects (
 id uuid PRIMARY KEY, project_key text UNIQUE NOT NULL, display_name text NOT NULL,
 parent_project_key text, repo_root text NOT NULL, repo_type text NOT NULL,
 status text NOT NULL, default_agent text, metadata jsonb,
 created_at timestamptz, updated_at timestamptz
);
CREATE TABLE public.cortex_project_paths (
 id uuid PRIMARY KEY, project_key text NOT NULL, root_path text NOT NULL,
 path_kind text NOT NULL, metadata jsonb, created_at timestamptz
);
GRANT SELECT ON public.cortex_projects, public.cortex_project_paths TO fixture_reader;
"""


def native_table(path: str, name: str) -> str:
    text = (ROOT / "migrations" / path).read_text()
    result = re.search(r"CREATE TABLE(?: IF NOT EXISTS)? " + re.escape(name)
                       + r" \(.*?\n\);", text, re.S)
    assert result, name
    return result.group(0)


def target_ddl() -> str:
    tables = [
        ("0001_core.sql", "cortex_core.scopes"),
        ("0001_core.sql", "cortex_core.scope_aliases"),
        ("0002_w1_identity_memory.sql", "cortex_auth.installations"),
        ("0011_keys_lead_sponsor.sql", "cortex_auth.project_installations"),
        ("0015_keys_project_create.sql", "cortex_core.project_registry"),
        ("0015_keys_project_create.sql", "cortex_core.project_identities"),
        ("0015_keys_project_create.sql", "cortex_core.project_profiles"),
    ]
    text = (ROOT / "migrations/0011_keys_lead_sponsor.sql").read_text()
    function = re.search(r"CREATE FUNCTION cortex_auth.check_project_binding\(\).*?\$function\$;",
                         text, re.S)
    trigger = re.search(r"CREATE TRIGGER project_installations[^;]*;", text, re.S)
    assert function and trigger
    ddl = "CREATE SCHEMA cortex_core; CREATE SCHEMA cortex_auth;\n"
    ddl += "\n".join(native_table(path, name) for path, name in tables)
    core = (ROOT / "migrations/0001_core.sql").read_text()
    alias_index = re.search(r"CREATE UNIQUE INDEX scope_aliases_one_primary_per_scope[^;]*;", core, re.S)
    assert alias_index
    ddl += "\n" + alias_index.group(0)
    ddl += "\n" + function.group(0) + "\n" + trigger.group(0)
    # This minimal fixture proves migrator effects, not the full app/RLS policy.
    for _, name in tables:
        ddl += (f"\nALTER TABLE {name} ENABLE ROW LEVEL SECURITY;"
                f"\nALTER TABLE {name} FORCE ROW LEVEL SECURITY;"
                f"\nCREATE POLICY fixture_migrator ON {name} FOR ALL TO cortex_v2_migrator "
                "USING (true) WITH CHECK (true);")
    ddl += ("\nINSERT INTO cortex_auth.installations(installation_id,display_name) "
            f"VALUES ('{INSTALLATION}','Unissued R148 fixture');")
    migration = ROOT / "migrations/0022_state_import.sql"
    if migration.exists():
        ddl += "\n" + migration.read_text()
    return ddl


@pytest.fixture(scope="session")
def state_cluster():
    parent = Path(os.environ["CORTEX_NATIVE_FIXTURE_ROOT"])
    info = parent.lstat()
    assert not parent.is_symlink() and info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert str(parent).startswith("/Users/amadmalik/DevVault/helix/tmp/")
    home = Path(tempfile.mkdtemp(prefix="p-", dir=parent))
    home.chmod(0o700)
    bins = Path(shutil.which("postgres") or "/opt/homebrew/bin/postgres").resolve().parent
    for name in ("postgres", "initdb", "pg_ctl"):
        assert (bins / name).is_file(), name
    env = os.environ.copy()
    for key in ("PGHOST", "PGHOSTADDR", "PGUSER", "PGDATABASE", "PGPORT",
                "PGPASSWORD", "PGSERVICE", "PGOPTIONS"):
        env.pop(key, None)
    env.update(PGPASSFILE="/dev/null", PGSERVICEFILE="/dev/null", PGSYSCONFDIR="/dev/null")
    def run(args):
        return subprocess.run([str(arg) for arg in args], env=env, capture_output=True,
                              timeout=40, check=True)
    assert b"18." in run([bins / "postgres", "--version"]).stdout
    socket = home / "s"
    socket.mkdir(mode=0o700)
    data = home / "data"
    started = False
    try:
        run([bins / "initdb", "-D", data, "-U", "fixture_owner", "-A", "trust",
             "--locale=C", "--encoding=UTF8"])
        options = "-h '' -k " + shlex.quote(str(socket)) + " -p 18623 -c max_connections=20"
        run([bins / "pg_ctl", "-D", data, "-l", home / "postgres.log",
             "-o", options, "-w", "start"])
        started = True
        async def connect(database, user):
            return await asyncpg.connect(host=str(socket), port=18623, database=database,
                                         user=user, password="", ssl=False, command_timeout=10)
        async def roles():
            conn = await connect("postgres", "fixture_owner")
            try:
                await conn.execute("CREATE ROLE cortex_v2_migrator LOGIN NOINHERIT; "
                                   "CREATE ROLE cortex_v2_app LOGIN NOINHERIT; "
                                   "CREATE ROLE fixture_reader LOGIN NOINHERIT;")
            finally:
                await conn.close()
        asyncio.run(roles())
        @asynccontextmanager
        async def pair():
            suffix = uuid.uuid4().hex
            source_name, target_name = "legacy_restore_" + suffix, "state_target_" + suffix
            admin = await connect("postgres", "fixture_owner")
            opened = []
            try:
                await admin.execute(f'CREATE DATABASE "{source_name}" OWNER fixture_owner')
                await admin.execute(f'CREATE DATABASE "{target_name}" OWNER cortex_v2_migrator')
                writer = await connect(source_name, "fixture_owner")
                opened.append(writer)
                await writer.execute(SOURCE_DDL)
                target = await connect(target_name, "cortex_v2_migrator")
                opened.append(target)
                await target.execute(target_ddl())
                source = await connect(source_name, "fixture_reader")
                opened.append(source)
                await source.execute("SET default_transaction_read_only=on")
                yield dict(writer=writer, source=source, target=target,
                           source_name=source_name, target_name=target_name, connect=connect)
            finally:
                for conn in reversed(opened):
                    await conn.close()
                for name in (source_name, target_name):
                    await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
                await admin.close()
        yield pair
    finally:
        if started:
            run([bins / "pg_ctl", "-D", data, "-m", "immediate", "-w", "stop"])
        shutil.rmtree(home)
