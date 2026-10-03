from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

SANDBOX_INSTANCE = "cortex-v2-v0-02-001-sandbox"
W1_INSTANCE = "cortex-v2-w1-candidate"
KAI_TEST_INSTANCE = "cortex_kai_test"
PRODUCTION_INSTANCE = "cortex_production"
EXPECTED_DATABASE = "cortex_v2"
EXPECTED_DATABASE_ROLE = "cortex_v2_app"
SECRET_DIRECTORY = Path("/run/secrets")
MIGRATE_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8,16}$")


@dataclass(frozen=True, slots=True)
class InstanceProfile:
    instance_id: str
    deployment_class: str
    database_host: str
    contract_version: str
    api_host: str
    api_port: int
    state_directory: str
    migrations: tuple[str, ...]
    operation_modules: tuple[str, ...] = ()
    container_prefix: str = ""
    resource_prefix: str = ""

    def secret_name(self, suffix: str) -> str:
        return f"{self.instance_id}-{suffix}"

    def container_name(self, role: str) -> str:
        if not self.container_prefix:
            raise ConfigurationError("profile declares no container naming plan")
        return f"{self.container_prefix}_{role}"

    def migrate_container_name(self, run_id: str) -> str:
        if not MIGRATE_RUN_ID_PATTERN.fullmatch(run_id or ""):
            raise ConfigurationError(
                "migrate run id must be 8-16 lowercase hex characters"
            )
        return f"{self.container_prefix}_migrate_{run_id}"

    def volume_name(self, suffix: str) -> str:
        if not self.resource_prefix:
            raise ConfigurationError("profile declares no resource naming plan")
        return f"{self.resource_prefix}_{suffix}"

    def network_name(self, suffix: str) -> str:
        return self.volume_name(suffix)


FULL_V2_MIGRATIONS: tuple[str, ...] = (
    "0001_core.sql",
    "0002_w1_identity_memory.sql",
    "0003_coordination.sql",
    "0004_processing.sql",
    "0005_retrieval.sql",
    "0006_context_interface.sql",
    "0007_feed_verification.sql",
    "0008_ops.sql",
    "0009_coordination_withdraw_and_target_fk.sql",
    "0010_keys_bootstrap.sql",
    "0011_keys_lead_sponsor.sql",
    "0012_keys_create_right.sql",
    "0013_keys_issuance_t21.sql",
    "0014_keys_365d_t34.sql",
)
FULL_V2_OPERATION_MODULES: tuple[str, ...] = (
    "cortex_v2.coordination.operations",
    "cortex_v2.processing.operations",
    "cortex_v2.retrieval.operations",
    "cortex_v2.interface.operations",
    "cortex_v2.ops.operations",
)
INTEGRATED_OPERATION_MODULES: tuple[str, ...] = (
    *FULL_V2_OPERATION_MODULES,
    "cortex_v2.ingest.operations",
    "cortex_v2.feed.operations",
    "cortex_v2.verification.operations",
)

INSTANCE_PROFILES: dict[str, InstanceProfile] = {
    SANDBOX_INSTANCE: InstanceProfile(
        instance_id=SANDBOX_INSTANCE,
        deployment_class="isolated-sandbox",
        database_host="db",
        api_host="api",
        api_port=8601,
        state_directory="cortex-v2-sandbox",
        contract_version="cortex.api.v2-slice1",
        migrations=("0001_core.sql",),
    ),
    W1_INSTANCE: InstanceProfile(
        instance_id=W1_INSTANCE,
        deployment_class="isolated-w1-candidate",
        database_host="w1-db",
        api_host="w1-api",
        api_port=8601,
        contract_version="cortex.api.v2-w1",
        state_directory="cortex-v2-w1-candidate",
        migrations=FULL_V2_MIGRATIONS,
        operation_modules=FULL_V2_OPERATION_MODULES,
    ),
    KAI_TEST_INSTANCE: InstanceProfile(
        instance_id=KAI_TEST_INSTANCE,
        deployment_class="isolated-kai-test",
        database_host="db",
        api_host="api",
        api_port=8601,
        contract_version="cortex.api.v2-integrated",
        state_directory="cortex-kai-test",
        migrations=FULL_V2_MIGRATIONS,
        operation_modules=INTEGRATED_OPERATION_MODULES,
        container_prefix="Cortex_kai_test",
        resource_prefix="cortex_kai_test",
    ),
    PRODUCTION_INSTANCE: InstanceProfile(
        instance_id=PRODUCTION_INSTANCE,
        deployment_class="production",
        database_host="db",
        api_host="api",
        api_port=8601,
        contract_version="cortex.api.v2-integrated",
        state_directory="cortex-production",
        migrations=FULL_V2_MIGRATIONS,
        operation_modules=INTEGRATED_OPERATION_MODULES,
        container_prefix="Cortex_production",
        resource_prefix="cortex_production",
    ),
}


class ConfigurationError(RuntimeError):
    pass


def active_profile() -> InstanceProfile:
    instance_id = os.environ.get("CORTEX_V2_SANDBOX_INSTANCE", "")
    profile = INSTANCE_PROFILES.get(instance_id)
    if profile is None:
        raise ConfigurationError("unknown Cortex v2 instance profile")
    return profile


def read_secret_path(name: str, *, minimum_bytes: int = 1) -> bytes:
    raw_path = os.environ.get(name)
    if not raw_path:
        raise ConfigurationError(f"required secret-file setting is missing: {name}")
    path = Path(raw_path)
    try:
        path.relative_to(SECRET_DIRECTORY)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(SECRET_DIRECTORY)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"secret file is unavailable: {name}") from exc
    if not resolved_path.is_file():
        raise ConfigurationError(f"secret file is unavailable: {name}")
    try:
        secret = resolved_path.read_bytes().strip()
    except OSError as exc:
        raise ConfigurationError(f"secret file is unavailable: {name}") from exc
    if len(secret) < minimum_bytes:
        raise ConfigurationError(f"secret file is too short: {name}")
    return secret


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    token_pepper: bytes
    instance_id: str
    installation_id: uuid.UUID | None = None

    @classmethod
    def from_env(cls) -> Settings:
        profile = active_profile()

        database_url = read_secret_path("CORTEX_V2_DATABASE_URL_FILE").decode()
        parsed = urlsplit(database_url)
        if (
            parsed.scheme != "postgresql"
            or unquote(parsed.username or "") != EXPECTED_DATABASE_ROLE
            or parsed.hostname != profile.database_host
            or parsed.path != f"/{EXPECTED_DATABASE}"
        ):
            raise ConfigurationError(
                "database URL does not identify the private v2 candidate"
            )

        pepper = bytes.fromhex(
            read_secret_path("CORTEX_V2_TOKEN_PEPPER_FILE", minimum_bytes=64).decode()
        )
        if len(pepper) < 32:
            raise ConfigurationError(
                "candidate token pepper must contain at least 32 bytes"
            )
        installation_id = None
        if profile.instance_id == PRODUCTION_INSTANCE:
            try:
                installation_id = uuid.UUID(
                    read_secret_path("CORTEX_V2_INSTALLATION_ID_FILE").decode()
                )
            except (UnicodeError, ValueError) as exc:
                raise ConfigurationError("installation identity is invalid") from exc
        return cls(
            database_url=database_url,
            token_pepper=pepper,
            instance_id=profile.instance_id,
            installation_id=installation_id,
        )
