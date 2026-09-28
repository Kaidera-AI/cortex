from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg

from .config import PRODUCTION_INSTANCE, active_profile, read_secret_path

MIGRATION_DIRECTORY = Path(__file__).resolve().parents[2] / "migrations"
SHARED_SEED_BODY = "Synthetic shared-scope fixture for Cortex v2 isolation tests."
UNGRANTED_SCOPE_ID = uuid.UUID("f3122ca3-5639-4d5d-8ce6-5fb11b234001")
UNGRANTED_RECORD_ID = uuid.UUID("f3122ca3-5639-4d5d-8ce6-5fb11b234002")
UNGRANTED_EVENT_ID = uuid.UUID("f3122ca3-5639-4d5d-8ce6-5fb11b234003")
UNGRANTED_ALIAS = "ungranted-probe"
UNGRANTED_SEED_BODY = "Synthetic ungranted-scope fixture for Cortex v2 isolation tests."
W1_CONNECTOR_NAMESPACE = "w1-session"
FIXTURE_UUID_FIELDS = (
    "principal_id",
    "installation_id",
    "credential_id",
    "project_scope_id",
    "shared_scope_id",
    "local_scope_id",
    "shared_record_id",
    "shared_event_id",
)
FIXTURE_ALIAS_FIELDS = ("project_alias", "shared_alias", "local_alias")
FIXTURE_REQUIRED_FIELDS = frozenset(
    (
        "instance_id",
        *FIXTURE_UUID_FIELDS,
        "credential_hash",
        "credential_generation",
        *FIXTURE_ALIAS_FIELDS,
    )
)
W1_FIXTURE_UUID_FIELDS = (
    "installation_id",
    "owner_principal_id",
    "worker_principal_id",
    "owner_actor_id",
    "worker_actor_id",
    "owner_credential_id",
    "worker_credential_id",
    "project_scope_id",
    "shared_scope_id",
    "local_scope_id",
    "ungranted_scope_id",
    "shared_record_id",
    "shared_event_id",
    "ungranted_record_id",
    "ungranted_event_id",
    "connector_id",
)
W1_FIXTURE_ALIAS_FIELDS = (
    "project_alias",
    "shared_alias",
    "local_alias",
    "ungranted_alias",
)
W1_FIXTURE_REQUIRED_FIELDS = frozenset(
    (
        "instance_id",
        *W1_FIXTURE_UUID_FIELDS,
        "owner_credential_hash",
        "worker_credential_hash",
        "credential_generation",
        "recovery_hash",
        "recovery_generation",
        *W1_FIXTURE_ALIAS_FIELDS,
    )
)


def _migrator_url(database_host: str) -> str:
    value = read_secret_path("CORTEX_V2_MIGRATOR_DATABASE_URL_FILE").decode()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "postgresql"
        or unquote(parsed.username or "") != "cortex_v2_migrator"
        or parsed.hostname != database_host
        or parsed.path != "/cortex_v2"
    ):
        raise RuntimeError("migration database URL is not the isolated v2 target")
    return value


def _fixture(profile, required_fields: frozenset[str]) -> dict[str, object]:
    try:
        fixture = json.loads(read_secret_path("CORTEX_V2_FIXTURE_FILE"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("candidate fixture is not valid JSON") from exc
    if not isinstance(fixture, dict) or not required_fields <= fixture.keys():
        raise RuntimeError("candidate fixture is incomplete")
    if fixture["instance_id"] != profile.instance_id:
        raise RuntimeError("candidate fixture has a different installation identity")
    return fixture


def _sandbox_fixture(profile) -> dict[str, object]:
    fixture = _fixture(profile, FIXTURE_REQUIRED_FIELDS)
    try:
        for field in FIXTURE_UUID_FIELDS:
            fixture[field] = str(uuid.UUID(str(fixture[field])))
        credential_hash = fixture["credential_hash"]
        if not isinstance(credential_hash, str):
            raise ValueError
        fixture["credential_hash"] = bytes.fromhex(credential_hash).hex()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "sandbox fixture contains an invalid identifier or credential hash"
        ) from exc

    if len(bytes.fromhex(str(fixture["credential_hash"]))) != 32:
        raise RuntimeError("sandbox fixture credential hash has the wrong length")
    _validated_generation(fixture, "credential_generation")
    _validated_aliases(fixture, FIXTURE_ALIAS_FIELDS)
    return fixture


def _w1_fixture(profile) -> dict[str, object]:
    fixture = _fixture(profile, W1_FIXTURE_REQUIRED_FIELDS)
    try:
        for field in W1_FIXTURE_UUID_FIELDS:
            fixture[field] = str(uuid.UUID(str(fixture[field])))
        for field in ("owner_credential_hash", "worker_credential_hash", "recovery_hash"):
            value = fixture[field]
            if not isinstance(value, str):
                raise ValueError
            fixture[field] = bytes.fromhex(value).hex()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "w1 fixture contains an invalid identifier or credential hash"
        ) from exc

    for field in ("owner_credential_hash", "worker_credential_hash", "recovery_hash"):
        if len(bytes.fromhex(str(fixture[field]))) != 32:
            raise RuntimeError(f"w1 fixture hash has the wrong length: {field}")
    _validated_generation(fixture, "credential_generation")
    _validated_generation(fixture, "recovery_generation")
    _validated_aliases(fixture, W1_FIXTURE_ALIAS_FIELDS)
    return fixture


def _validated_generation(fixture: dict[str, object], field: str) -> None:
    generation = fixture[field]
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise RuntimeError(f"candidate fixture generation is invalid: {field}")


def _validated_aliases(fixture: dict[str, object], fields: tuple[str, ...]) -> None:
    aliases = [fixture[field] for field in fields]
    if (
        any(
            not isinstance(alias, str)
            or not 1 <= len(alias) <= 96
            or "\x00" in alias
            for alias in aliases
        )
        or len(set(aliases)) != len(aliases)
    ):
        raise RuntimeError("candidate fixture aliases are invalid")


async def _assert_role_contract(connection: asyncpg.Connection) -> None:
    roles = await connection.fetch(
        """
        SELECT rolname, rolcanlogin, rolbypassrls, rolsuper, rolcreaterole,
               rolcreatedb
          FROM pg_roles
         WHERE rolname IN ('cortex_v2_app', 'cortex_v2_migrator')
        """
    )
    roles_by_name = {row["rolname"]: row for row in roles}
    app_role = roles_by_name.get("cortex_v2_app")
    migrator_role = roles_by_name.get("cortex_v2_migrator")
    if (
        app_role is None
        or not app_role["rolcanlogin"]
        or app_role["rolbypassrls"]
        or app_role["rolsuper"]
        or app_role["rolcreaterole"]
        or app_role["rolcreatedb"]
        or migrator_role is None
        or not migrator_role["rolcanlogin"]
        or migrator_role["rolbypassrls"]
        or migrator_role["rolsuper"]
    ):
        raise RuntimeError(
            "candidate database roles do not satisfy the isolated RLS contract"
        )

    if await connection.fetchval(
        "SELECT pg_has_role('cortex_v2_app', 'cortex_v2_migrator', 'member')"
    ):
        raise RuntimeError("candidate app role must not inherit the migrator role")

    schema_owners = await connection.fetch(
        """
        SELECT nspname, pg_get_userbyid(nspowner) AS owner
          FROM pg_namespace
         WHERE nspname IN ('cortex_auth', 'cortex_core')
        """
    )
    if {row["nspname"]: row["owner"] for row in schema_owners} != {
        "cortex_auth": "cortex_v2_migrator",
        "cortex_core": "cortex_v2_migrator",
    }:
        raise RuntimeError("candidate schemas must be owned by the migrator role")

    if await connection.fetchval(
        """
        SELECT count(*)
          FROM pg_class AS relation
          JOIN pg_roles AS owner ON owner.oid = relation.relowner
         WHERE relation.relnamespace IN (
                   'cortex_auth'::regnamespace,
                   'cortex_core'::regnamespace
               )
           AND relation.relkind IN ('r', 'p')
           AND owner.rolname = 'cortex_v2_app'
        """
    ):
        raise RuntimeError("candidate app role must not own candidate relations")


async def apply() -> None:
    profile = active_profile()
    fixture = None
    if profile.instance_id != PRODUCTION_INSTANCE:
        if "0002_w1_identity_memory.sql" in profile.migrations:
            fixture = _w1_fixture(profile)
        else:
            fixture = _sandbox_fixture(profile)
    migrations = []
    for migration_id in profile.migrations:
        migration_path = MIGRATION_DIRECTORY / migration_id
        if not migration_path.is_file():
            raise RuntimeError(
                f"registered migration is missing from this build: {migration_id}"
            )
        sql = migration_path.read_text()
        migrations.append(
            (migration_id, hashlib.sha256(sql.encode("utf-8")).hexdigest(), sql)
        )
    connection = await asyncpg.connect(
        _migrator_url(profile.database_host), command_timeout=60
    )
    try:
        await connection.execute(
            "CREATE SCHEMA IF NOT EXISTS cortex_auth "
            "AUTHORIZATION cortex_v2_migrator"
        )
        await connection.execute(
            "CREATE SCHEMA IF NOT EXISTS cortex_core "
            "AUTHORIZATION cortex_v2_migrator"
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cortex_core.schema_migrations (
                migration_id text PRIMARY KEY,
                checksum_sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        status = "verified"
        for migration_id, checksum, sql in migrations:
            recorded = await connection.fetchval(
                "SELECT checksum_sha256 FROM cortex_core.schema_migrations "
                "WHERE migration_id = $1",
                migration_id,
            )
            if recorded is not None and recorded != checksum:
                raise RuntimeError(f"checksum mismatch for migration {migration_id}")
            if recorded is None:
                status = "applied"
                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO cortex_core.schema_migrations"
                        "(migration_id, checksum_sha256) VALUES ($1, $2)",
                        migration_id,
                        checksum,
                    )
        await _assert_role_contract(connection)
        if fixture is not None:
            if "0002_w1_identity_memory.sql" in profile.migrations:
                await _seed_w1(connection, fixture)
            else:
                await _seed_sandbox(connection, fixture)
        print(
            json.dumps(
                {
                    "instance": profile.instance_id,
                    "migrations": [
                        {"migration": migration_id, "checksum": checksum}
                        for migration_id, checksum, _ in migrations
                    ],
                    "status": status,
                    "fixture": "none" if fixture is None else "verified",
                }
            )
        )
    finally:
        await connection.close()


async def _seed_sandbox(
    connection: asyncpg.Connection, fixture: dict[str, object]
) -> None:
    async with connection.transaction():
        await connection.execute(
            """
            INSERT INTO cortex_auth.principals(
                principal_id, installation_id, principal_name, status
            )
            VALUES ($1, $2, 'sandbox-worker', 'active')
            ON CONFLICT (principal_id) DO NOTHING
            """,
            fixture["principal_id"],
            fixture["installation_id"],
        )
        await connection.execute(
            """
            INSERT INTO cortex_auth.credentials(
                credential_id, principal_id, token_hash, generation
            )
            VALUES ($1, $2, decode($3, 'hex'), $4)
            ON CONFLICT (credential_id) DO NOTHING
            """,
            fixture["credential_id"],
            fixture["principal_id"],
            fixture["credential_hash"],
            fixture["credential_generation"],
        )
        scopes = (
            (
                fixture["project_scope_id"],
                "project",
                "V2 Sandbox Project",
                fixture["project_alias"],
            ),
            (
                fixture["shared_scope_id"],
                "shared",
                "V2 Shared Test Library",
                fixture["shared_alias"],
            ),
            (
                fixture["local_scope_id"],
                "local",
                "V2 Local Test State",
                fixture["local_alias"],
            ),
            (
                UNGRANTED_SCOPE_ID,
                "project",
                "V2 Ungranted Probe",
                UNGRANTED_ALIAS,
            ),
        )
        for scope_id, kind, display_name, alias in scopes:
            await connection.execute(
                """
                INSERT INTO cortex_core.scopes(scope_id, scope_kind, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (scope_id) DO NOTHING
                """,
                scope_id,
                kind,
                display_name,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.scope_aliases(alias, scope_id, is_primary)
                VALUES ($1, $2, true)
                ON CONFLICT (alias) DO NOTHING
                """,
                alias,
                scope_id,
            )
        for scope_id, can_read, can_write, can_publish in (
            (fixture["project_scope_id"], True, True, False),
            (fixture["shared_scope_id"], True, False, False),
            (fixture["local_scope_id"], True, True, False),
            (UNGRANTED_SCOPE_ID, False, False, False),
        ):
            await connection.execute(
                """
                INSERT INTO cortex_auth.scope_grants(
                    principal_id, scope_id, can_read, can_write, can_publish
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (principal_id, scope_id) DO NOTHING
                """,
                fixture["principal_id"],
                scope_id,
                can_read,
                can_write,
                can_publish,
            )

        records = (
            (
                fixture["shared_scope_id"],
                fixture["shared_record_id"],
                fixture["shared_event_id"],
                SHARED_SEED_BODY,
            ),
            (
                UNGRANTED_SCOPE_ID,
                UNGRANTED_RECORD_ID,
                UNGRANTED_EVENT_ID,
                UNGRANTED_SEED_BODY,
            ),
        )
        for scope_id, record_id, event_id, body in records:
            await connection.execute(
                """
                INSERT INTO cortex_core.memory_records
                    (scope_id, record_id, logical_record_id, record_type, revision,
                     author_principal_id, body)
                VALUES ($1, $2, $2, 'knowledge', 1, $3, $4)
                ON CONFLICT (scope_id, record_id) DO NOTHING
                """,
                scope_id,
                record_id,
                fixture["principal_id"],
                body,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.lexical_documents(
                    scope_id, record_id, revision, source_text
                )
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (scope_id, record_id) DO NOTHING
                """,
                scope_id,
                record_id,
                body,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.outbox_events(
                    event_id, scope_id, aggregate_id, event_type, payload
                )
                VALUES ($1, $2, $3, 'memory.recorded', $4::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                event_id,
                scope_id,
                record_id,
                json.dumps(
                    {"record_id": str(record_id), "seed": "sandbox-only"}
                ),
            )
        await _assert_seed(connection, fixture)


async def _seed_w1(
    connection: asyncpg.Connection, fixture: dict[str, object]
) -> None:
    async with connection.transaction():
        await connection.execute(
            """
            INSERT INTO cortex_auth.installations(installation_id, display_name)
            VALUES ($1, 'Cortex v2 W1 Local Candidate')
            ON CONFLICT (installation_id) DO NOTHING
            """,
            fixture["installation_id"],
        )
        for principal_field, principal_name in (
            ("owner_principal_id", "w1-owner"),
            ("worker_principal_id", "w1-worker"),
        ):
            await connection.execute(
                """
                INSERT INTO cortex_auth.principals(
                    principal_id, installation_id, principal_name, status
                )
                VALUES ($1, $2, $3, 'active')
                ON CONFLICT (principal_id) DO NOTHING
                """,
                fixture[principal_field],
                fixture["installation_id"],
                principal_name,
            )
        await connection.execute(
            """
            INSERT INTO cortex_auth.installation_owners(installation_id, principal_id)
            VALUES ($1, $2)
            ON CONFLICT (installation_id, principal_id) DO NOTHING
            """,
            fixture["installation_id"],
            fixture["owner_principal_id"],
        )
        await connection.execute(
            """
            INSERT INTO cortex_auth.installation_recovery(
                installation_id, recovery_token_hash, generation
            )
            VALUES ($1, decode($2, 'hex'), $3)
            ON CONFLICT (installation_id) DO NOTHING
            """,
            fixture["installation_id"],
            fixture["recovery_hash"],
            fixture["recovery_generation"],
        )
        for actor_field, actor_kind, display_name in (
            ("owner_actor_id", "human", "w1-owner"),
            ("worker_actor_id", "agent", "w1-worker"),
        ):
            await connection.execute(
                """
                INSERT INTO cortex_auth.actors(
                    actor_id, installation_id, actor_kind, display_name
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (actor_id) DO NOTHING
                """,
                fixture[actor_field],
                fixture["installation_id"],
                actor_kind,
                display_name,
            )
        for actor_field, principal_field in (
            ("owner_actor_id", "owner_principal_id"),
            ("worker_actor_id", "worker_principal_id"),
        ):
            await connection.execute(
                """
                INSERT INTO cortex_auth.actor_bindings(
                    actor_id, principal_id, bound_by_principal_id
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (actor_id) DO NOTHING
                """,
                fixture[actor_field],
                fixture[principal_field],
                fixture["owner_principal_id"],
            )
        for credential_field, principal_field, hash_field in (
            (
                "owner_credential_id",
                "owner_principal_id",
                "owner_credential_hash",
            ),
            (
                "worker_credential_id",
                "worker_principal_id",
                "worker_credential_hash",
            ),
        ):
            await connection.execute(
                """
                INSERT INTO cortex_auth.credentials(
                    credential_id, principal_id, token_hash, generation
                )
                VALUES ($1, $2, decode($3, 'hex'), $4)
                ON CONFLICT (credential_id) DO NOTHING
                """,
                fixture[credential_field],
                fixture[principal_field],
                fixture[hash_field],
                fixture["credential_generation"],
            )
        scopes = (
            (
                fixture["project_scope_id"],
                "project",
                "W1 Candidate Project",
                fixture["project_alias"],
            ),
            (
                fixture["shared_scope_id"],
                "shared",
                "W1 Shared Test Library",
                fixture["shared_alias"],
            ),
            (
                fixture["local_scope_id"],
                "local",
                "W1 Local Test State",
                fixture["local_alias"],
            ),
            (
                fixture["ungranted_scope_id"],
                "project",
                "W1 Ungranted Probe",
                fixture["ungranted_alias"],
            ),
        )
        for scope_id, kind, display_name, alias in scopes:
            await connection.execute(
                """
                INSERT INTO cortex_core.scopes(scope_id, scope_kind, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (scope_id) DO NOTHING
                """,
                scope_id,
                kind,
                display_name,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.scope_aliases(alias, scope_id, is_primary)
                VALUES ($1, $2, true)
                ON CONFLICT (alias) DO NOTHING
                """,
                alias,
                scope_id,
            )
        owner_grants = (
            (fixture["project_scope_id"], True, True, True),
            (fixture["shared_scope_id"], True, True, True),
            (fixture["local_scope_id"], True, True, True),
        )
        worker_grants = (
            (fixture["project_scope_id"], True, True, False),
            (fixture["shared_scope_id"], True, False, False),
            (fixture["local_scope_id"], True, True, False),
            (fixture["ungranted_scope_id"], False, False, False),
        )
        for principal_field, grants in (
            ("owner_principal_id", owner_grants),
            ("worker_principal_id", worker_grants),
        ):
            for scope_id, can_read, can_write, can_publish in grants:
                await connection.execute(
                    """
                    INSERT INTO cortex_auth.scope_grants(
                        principal_id, scope_id, can_read, can_write, can_publish
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (principal_id, scope_id) DO NOTHING
                    """,
                    fixture[principal_field],
                    scope_id,
                    can_read,
                    can_write,
                    can_publish,
                )

        records = (
            (
                fixture["shared_scope_id"],
                fixture["shared_record_id"],
                fixture["shared_event_id"],
                SHARED_SEED_BODY,
            ),
            (
                fixture["ungranted_scope_id"],
                fixture["ungranted_record_id"],
                fixture["ungranted_event_id"],
                UNGRANTED_SEED_BODY,
            ),
        )
        for scope_id, record_id, event_id, body in records:
            await connection.execute(
                """
                INSERT INTO cortex_core.memory_records
                    (scope_id, record_id, logical_record_id, record_type, revision,
                     author_principal_id, body)
                VALUES ($1, $2, $2, 'knowledge', 1, $3, $4)
                ON CONFLICT (scope_id, record_id) DO NOTHING
                """,
                scope_id,
                record_id,
                fixture["worker_principal_id"],
                body,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.lexical_documents(
                    scope_id, record_id, revision, source_text
                )
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (scope_id, record_id) DO NOTHING
                """,
                scope_id,
                record_id,
                body,
            )
            await connection.execute(
                """
                INSERT INTO cortex_core.outbox_events(
                    event_id, scope_id, aggregate_id, event_type, payload
                )
                VALUES ($1, $2, $3, 'memory.recorded', $4::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                event_id,
                scope_id,
                record_id,
                json.dumps({"record_id": str(record_id), "seed": "w1-candidate-only"}),
            )
        await connection.execute(
            """
            INSERT INTO cortex_core.source_connectors(
                connector_id, namespace, connector_kind, installation_id
            )
            VALUES ($1, $2, 'session_ingest', $3)
            ON CONFLICT (connector_id) DO NOTHING
            """,
            fixture["connector_id"],
            W1_CONNECTOR_NAMESPACE,
            fixture["installation_id"],
        )
        await _assert_seed_w1(connection, fixture)


async def _assert_seed_w1(
    connection: asyncpg.Connection, fixture: dict[str, object]
) -> None:
    """Verify the fixture baseline row-by-row, scoped to the fixture's own
    ids. The integrated lane database legitimately accumulates other
    installations (module suites seed their own) and lane history (rotated
    credentials, retired alias rows, restored grants), so whole-table
    equality would reject valid history; every fixture row must still exist
    with its seeded semantics or the seed has drifted."""
    installation = await connection.fetchrow(
        "SELECT installation_id, status FROM cortex_auth.installations "
        "WHERE installation_id = $1",
        fixture["installation_id"],
    )
    if installation is None or installation["status"] != "active":
        raise RuntimeError("w1 installation differs from its private fixture")

    owner = await connection.fetchrow(
        "SELECT principal_id, revoked_at FROM cortex_auth.installation_owners "
        "WHERE installation_id = $1 AND principal_id = $2",
        fixture["installation_id"],
        fixture["owner_principal_id"],
    )
    if owner is None or owner["revoked_at"] is not None:
        raise RuntimeError("w1 installation owner differs from its private fixture")

    recovery = await connection.fetchrow(
        "SELECT recovery_token_hash, generation "
        "FROM cortex_auth.installation_recovery WHERE installation_id = $1",
        fixture["installation_id"],
    )
    if recovery is None or recovery["generation"] < fixture["recovery_generation"]:
        raise RuntimeError("w1 recovery credential differs from its private fixture")
    if (
        recovery["generation"] == fixture["recovery_generation"]
        and recovery["recovery_token_hash"].hex() != fixture["recovery_hash"]
    ):
        raise RuntimeError("w1 recovery credential differs from its private fixture")

    principals = await connection.fetch(
        "SELECT principal_id, principal_name, status FROM cortex_auth.principals "
        "WHERE principal_id = ANY($1::uuid[])",
        [fixture["owner_principal_id"], fixture["worker_principal_id"]],
    )
    actual_principals = {
        str(row["principal_id"]): (row["principal_name"], row["status"])
        for row in principals
    }
    if actual_principals != {
        str(fixture["owner_principal_id"]): ("w1-owner", "active"),
        str(fixture["worker_principal_id"]): ("w1-worker", "active"),
    }:
        raise RuntimeError("w1 principals differ from their private fixture")

    actors = await connection.fetch(
        "SELECT actor_id, actor_kind FROM cortex_auth.actors "
        "WHERE actor_id = ANY($1::uuid[])",
        [fixture["owner_actor_id"], fixture["worker_actor_id"]],
    )
    if {str(row["actor_id"]): row["actor_kind"] for row in actors} != {
        str(fixture["owner_actor_id"]): "human",
        str(fixture["worker_actor_id"]): "agent",
    }:
        raise RuntimeError("w1 actors differ from their private fixture")

    bindings = await connection.fetchval(
        "SELECT count(*) FROM cortex_auth.actor_bindings "
        "WHERE principal_id = ANY($1::uuid[])",
        [fixture["owner_principal_id"], fixture["worker_principal_id"]],
    )
    if bindings != 2:
        raise RuntimeError("w1 actor bindings differ from their private fixture")

    credentials = await connection.fetch(
        "SELECT credential_id, principal_id, token_hash, generation, revoked_at "
        "FROM cortex_auth.credentials WHERE credential_id = ANY($1::uuid[])",
        [fixture["owner_credential_id"], fixture["worker_credential_id"]],
    )
    actual_credentials = {
        str(row["credential_id"]): row for row in credentials
    }
    for credential_id, principal_id, token_hash in (
        (
            str(fixture["owner_credential_id"]),
            str(fixture["owner_principal_id"]),
            fixture["owner_credential_hash"],
        ),
        (
            str(fixture["worker_credential_id"]),
            str(fixture["worker_principal_id"]),
            fixture["worker_credential_hash"],
        ),
    ):
        row = actual_credentials.get(credential_id)
        if row is None or str(row["principal_id"]) != principal_id:
            raise RuntimeError("w1 credentials differ from their private fixture")
        if (
            row["generation"] == fixture["credential_generation"]
            and row["revoked_at"] is None
            and row["token_hash"].hex() != token_hash
        ):
            raise RuntimeError("w1 credentials differ from their private fixture")

    scopes = await connection.fetch(
        "SELECT scope_id, scope_kind, is_active FROM cortex_core.scopes "
        "WHERE scope_id = ANY($1::uuid[])",
        [
            fixture["project_scope_id"],
            fixture["shared_scope_id"],
            fixture["local_scope_id"],
            fixture["ungranted_scope_id"],
        ],
    )
    if {
        str(row["scope_id"]): (row["scope_kind"], row["is_active"]) for row in scopes
    } != {
        str(fixture["project_scope_id"]): ("project", True),
        str(fixture["shared_scope_id"]): ("shared", True),
        str(fixture["local_scope_id"]): ("local", True),
        str(fixture["ungranted_scope_id"]): ("project", True),
    }:
        raise RuntimeError("w1 scope registry differs from its private fixture")

    aliases = await connection.fetch(
        "SELECT alias, scope_id, is_primary, retired_at FROM cortex_core.scope_aliases "
        "WHERE scope_id = ANY($1::uuid[])",
        [
            fixture["project_scope_id"],
            fixture["shared_scope_id"],
            fixture["local_scope_id"],
            fixture["ungranted_scope_id"],
        ],
    )
    primary_by_scope: dict[str, list[str]] = {}
    alias_rows = {}
    for row in aliases:
        alias_rows[row["alias"]] = str(row["scope_id"])
        if row["is_primary"] and row["retired_at"] is None:
            primary_by_scope.setdefault(str(row["scope_id"]), []).append(row["alias"])
    # A rename test may leave a fixture scope under a new primary alias (the
    # seeded alias stays as a resolving legacy row); what must hold is one
    # live primary per fixture scope and every seeded alias still bound to
    # its own scope.
    if set(primary_by_scope) != {
        str(fixture["project_scope_id"]),
        str(fixture["shared_scope_id"]),
        str(fixture["local_scope_id"]),
        str(fixture["ungranted_scope_id"]),
    } or any(len(names) != 1 for names in primary_by_scope.values()):
        raise RuntimeError("w1 aliases differ from their private fixture")
    for alias_name, scope_id in (
        (str(fixture["project_alias"]), str(fixture["project_scope_id"])),
        (str(fixture["shared_alias"]), str(fixture["shared_scope_id"])),
        (str(fixture["local_alias"]), str(fixture["local_scope_id"])),
        (str(fixture["ungranted_alias"]), str(fixture["ungranted_scope_id"])),
    ):
        if alias_rows.get(alias_name) != scope_id:
            raise RuntimeError("w1 aliases differ from their private fixture")

    grants = await connection.fetch(
        "SELECT principal_id, scope_id, can_read, can_write, can_publish, revoked_at "
        "FROM cortex_auth.scope_grants "
        "WHERE principal_id = ANY($1::uuid[]) AND scope_id = ANY($2::uuid[])",
        [fixture["owner_principal_id"], fixture["worker_principal_id"]],
        [
            fixture["project_scope_id"],
            fixture["shared_scope_id"],
            fixture["local_scope_id"],
            fixture["ungranted_scope_id"],
        ],
    )
    live_grants = {
        (str(row["principal_id"]), str(row["scope_id"])): (
            row["can_read"],
            row["can_write"],
            row["can_publish"],
        )
        for row in grants
        if row["revoked_at"] is None
    }
    owner_id = str(fixture["owner_principal_id"])
    worker_id = str(fixture["worker_principal_id"])
    project = str(fixture["project_scope_id"])
    shared = str(fixture["shared_scope_id"])
    local = str(fixture["local_scope_id"])
    ungranted = str(fixture["ungranted_scope_id"])
    if live_grants != {
        (owner_id, project): (True, True, True),
        (owner_id, shared): (True, True, True),
        (owner_id, local): (True, True, True),
        (worker_id, project): (True, True, False),
        (worker_id, shared): (True, False, False),
        (worker_id, local): (True, True, False),
        (worker_id, ungranted): (False, False, False),
    }:
        raise RuntimeError("w1 scope grants differ from their private fixture")

    for scope_id, record_id, event_id, body in (
        (
            fixture["shared_scope_id"],
            fixture["shared_record_id"],
            fixture["shared_event_id"],
            SHARED_SEED_BODY,
        ),
        (
            fixture["ungranted_scope_id"],
            fixture["ungranted_record_id"],
            fixture["ungranted_event_id"],
            UNGRANTED_SEED_BODY,
        ),
    ):
        record = await connection.fetchrow(
            """
            SELECT record_type, revision, author_principal_id, body
              FROM cortex_core.memory_records
             WHERE scope_id = $1 AND record_id = $2
            """,
            scope_id,
            record_id,
        )
        if (
            record is None
            or record["record_type"] != "knowledge"
            or record["revision"] != 1
            or str(record["author_principal_id"]) != str(fixture["worker_principal_id"])
            or record["body"] != body
        ):
            raise RuntimeError("w1 fixture record differs from its private fixture")
        event = await connection.fetchval(
            """
            SELECT event_type FROM cortex_core.outbox_events
             WHERE event_id = $1 AND scope_id = $2 AND aggregate_id = $3
            """,
            event_id,
            scope_id,
            record_id,
        )
        if event != "memory.recorded":
            raise RuntimeError("w1 fixture outbox differs from its private fixture")

    connector = await connection.fetchrow(
        "SELECT connector_id, namespace, connector_kind, status "
        "FROM cortex_core.source_connectors WHERE connector_id = $1",
        fixture["connector_id"],
    )
    if (
        connector is None
        or connector["namespace"] != W1_CONNECTOR_NAMESPACE
        or connector["connector_kind"] != "session_ingest"
        or connector["status"] != "active"
    ):
        raise RuntimeError("w1 source connector differs from its private fixture")


async def _assert_seed(
    connection: asyncpg.Connection, fixture: dict[str, object]
) -> None:
    principals = await connection.fetch(
        """
        SELECT principal_id, installation_id, principal_name, status
          FROM cortex_auth.principals
        """
    )
    if (
        len(principals) != 1
        or str(principals[0]["principal_id"]) != str(fixture["principal_id"])
        or str(principals[0]["installation_id"]) != str(fixture["installation_id"])
        or principals[0]["principal_name"] != "sandbox-worker"
        or principals[0]["status"] != "active"
    ):
        raise RuntimeError("sandbox principal differs from its private fixture")

    credentials = await connection.fetch(
        """
        SELECT credential_id, principal_id, token_hash, generation, expires_at,
               revoked_at
          FROM cortex_auth.credentials
        """
    )
    if (
        len(credentials) != 1
        or str(credentials[0]["credential_id"]) != str(fixture["credential_id"])
        or str(credentials[0]["principal_id"]) != str(fixture["principal_id"])
        or credentials[0]["token_hash"].hex() != fixture["credential_hash"]
        or credentials[0]["generation"] != fixture["credential_generation"]
        or credentials[0]["expires_at"] is not None
        or credentials[0]["revoked_at"] is not None
    ):
        raise RuntimeError("sandbox credential differs from its private fixture")

    expected_scopes = {
        str(fixture["project_scope_id"]): ("project", "V2 Sandbox Project", True),
        str(fixture["shared_scope_id"]): ("shared", "V2 Shared Test Library", True),
        str(fixture["local_scope_id"]): ("local", "V2 Local Test State", True),
        str(UNGRANTED_SCOPE_ID): ("project", "V2 Ungranted Probe", True),
    }
    scopes = await connection.fetch(
        "SELECT scope_id, scope_kind, display_name, is_active FROM cortex_core.scopes"
    )
    actual_scopes = {
        str(row["scope_id"]): (
            row["scope_kind"],
            row["display_name"],
            row["is_active"],
        )
        for row in scopes
    }
    if actual_scopes != expected_scopes:
        raise RuntimeError("sandbox scope registry differs from its private fixture")

    expected_aliases = {
        str(fixture["project_alias"]): (str(fixture["project_scope_id"]), True, True),
        str(fixture["shared_alias"]): (str(fixture["shared_scope_id"]), True, True),
        str(fixture["local_alias"]): (str(fixture["local_scope_id"]), True, True),
        UNGRANTED_ALIAS: (str(UNGRANTED_SCOPE_ID), True, True),
    }
    aliases = await connection.fetch(
        "SELECT alias, scope_id, is_primary, retired_at FROM cortex_core.scope_aliases"
    )
    actual_aliases = {
        row["alias"]: (
            str(row["scope_id"]),
            row["is_primary"],
            row["retired_at"] is None,
        )
        for row in aliases
    }
    if actual_aliases != expected_aliases:
        raise RuntimeError("sandbox aliases differ from their private fixture")

    expected_grants = {
        (str(fixture["principal_id"]), str(fixture["project_scope_id"])): (
            True,
            True,
            False,
            True,
        ),
        (str(fixture["principal_id"]), str(fixture["shared_scope_id"])): (
            True,
            False,
            False,
            True,
        ),
        (str(fixture["principal_id"]), str(fixture["local_scope_id"])): (
            True,
            True,
            False,
            True,
        ),
        (str(fixture["principal_id"]), str(UNGRANTED_SCOPE_ID)): (
            False,
            False,
            False,
            True,
        ),
    }
    grants = await connection.fetch(
        """
        SELECT principal_id, scope_id, can_read, can_write, can_publish, revoked_at
          FROM cortex_auth.scope_grants
        """
    )
    actual_grants = {
        (str(row["principal_id"]), str(row["scope_id"])): (
            row["can_read"],
            row["can_write"],
            row["can_publish"],
            row["revoked_at"] is None,
        )
        for row in grants
    }
    if actual_grants != expected_grants:
        raise RuntimeError("sandbox scope grants differ from their private fixture")

    expected_records = (
        (
            fixture["shared_scope_id"],
            fixture["shared_record_id"],
            fixture["shared_event_id"],
            SHARED_SEED_BODY,
        ),
        (
            UNGRANTED_SCOPE_ID,
            UNGRANTED_RECORD_ID,
            UNGRANTED_EVENT_ID,
            UNGRANTED_SEED_BODY,
        ),
    )
    for scope_id, record_id, event_id, body in expected_records:
        record = await connection.fetchrow(
            """
            SELECT logical_record_id, record_type, revision, author_principal_id, body,
                   supersedes_id
              FROM cortex_core.memory_records
             WHERE scope_id = $1 AND record_id = $2
            """,
            scope_id,
            record_id,
        )
        if (
            record is None
            or str(record["logical_record_id"]) != str(record_id)
            or record["record_type"] != "knowledge"
            or record["revision"] != 1
            or str(record["author_principal_id"]) != str(fixture["principal_id"])
            or record["body"] != body
            or record["supersedes_id"] is not None
        ):
            raise RuntimeError(
                "sandbox fixture record differs from its private fixture"
            )

        lexical = await connection.fetchval(
            """
            SELECT source_text FROM cortex_core.lexical_documents
             WHERE scope_id = $1 AND record_id = $2 AND revision = 1
            """,
            scope_id,
            record_id,
        )
        event = await connection.fetchrow(
            """
            SELECT event_type, payload
              FROM cortex_core.outbox_events
             WHERE event_id = $1 AND scope_id = $2 AND aggregate_id = $3
            """,
            event_id,
            scope_id,
            record_id,
        )
        if event is None:
            event_payload = None
        elif isinstance(event["payload"], str):
            event_payload = json.loads(event["payload"])
        else:
            event_payload = event["payload"]
        if (
            lexical != body
            or event is None
            or event["event_type"] != "memory.recorded"
            or event_payload != {"record_id": str(record_id), "seed": "sandbox-only"}
        ):
            raise RuntimeError(
                "sandbox fixture record projections differ from their private fixture"
            )


def main() -> None:
    asyncio.run(apply())


if __name__ == "__main__":
    main()
