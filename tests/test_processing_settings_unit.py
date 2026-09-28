"""Processing settings unit tests: fail-closed validation (R24/R25), pure
``parse_settings`` over an env mapping, secret references by name only, and
``from_env`` boot resolution through ``cortex_v2.config.read_secret_path``.

Pure unit tests: no network, no database. ``from_env`` cases run against
monkeypatched environment and (for the success path) a monkeypatched
``read_secret_path``; no real secret is ever created.
"""

from __future__ import annotations

import json

import pytest

from cortex_v2 import config
from cortex_v2.config import ConfigurationError
from cortex_v2.processing.contracts import EXECUTOR_ROLES, ParserLimits
from cortex_v2.processing.providers import EgressPolicy
from cortex_v2.processing.settings import (
    MODEL_STATUSES,
    PROVIDER_KINDS,
    ROUTING_POLICIES,
    ProcessingSettings,
    parse_settings,
)

MODELS_JSON = json.dumps(
    [
        {
            "model_id": "nomic-embed-text",
            "digest": "sha256:9f2c41de",
            "license": "apache-2.0",
            "platform": "linux/arm64",
            "status": "installed",
            "dimensions": 768,
        },
        {
            "model_id": "qwen3:8b",
            "digest": "sha256:1a2b3c4d",
            "license": "other",
            "platform": "linux/arm64",
            "status": "declared",
        },
    ]
)


def valid_env() -> dict[str, str]:
    return {
        "CORTEX_V2_WORKER_ROLE": "embed",
        "CORTEX_V2_WORKER_ID": "embed-worker-01",
        "CORTEX_V2_WORKER_DATABASE_URL_FILE_ENV": "CORTEX_V2_DATABASE_URL_FILE",
        "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE_ENV": "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE",
        "CORTEX_V2_DATABASE_URL_FILE": "/run/secrets/cortex-v2-database-url",
        "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE": "/run/secrets/cortex-v2-worker-principal",
        "CORTEX_V2_WORKER_EXECUTOR_ROLES": "core,doc",
        "CORTEX_V2_WORKER_HANDLER_MODULES": (
            "cortex_v2.processing.handlers,cortex_v2.retrieval.graph"
        ),
        "CORTEX_V2_WORKER_POLL_INTERVAL_SECONDS": "1.5",
        "CORTEX_V2_WORKER_LEASE_SECONDS": "120",
        "CORTEX_V2_WORKER_HEARTBEAT_INTERVAL_SECONDS": "15",
        "CORTEX_V2_WORKER_ATTEMPT_DEADLINE_SECONDS": "900",
        "CORTEX_V2_WORKER_MAX_CONCURRENT_ATTEMPTS": "4",
        "CORTEX_V2_WORKER_THREAD_POOL_SIZE": "8",
        "CORTEX_V2_WORKER_BACKOFF_BASE_SECONDS": "2",
        "CORTEX_V2_WORKER_BACKOFF_CAP_SECONDS": "300",
        "CORTEX_V2_WORKER_QUEUE_ADMISSION_LIMIT": "1000",
        "CORTEX_V2_WORKER_REAP_INTERVAL_SECONDS": "30",
        "CORTEX_V2_WORKER_BLOB_ROOT": "/var/lib/cortex-v2/blobs",
        "CORTEX_V2_PROVIDER_EMBEDDING_ENABLED": "true",
        "CORTEX_V2_PROVIDER_EMBEDDING_KIND": "openai-compatible",
        "CORTEX_V2_PROVIDER_EMBEDDING_BASE_URL": "https://api.embedding.test/v1",
        "CORTEX_V2_PROVIDER_EMBEDDING_MODEL": "embed-large@2",
        "CORTEX_V2_PROVIDER_EMBEDDING_CREDENTIAL_ENV": "CORTEX_V2_EMBEDDING_API_KEY",
        "CORTEX_V2_PROVIDER_EMBEDDING_CONNECT_TIMEOUT_SECONDS": "3",
        "CORTEX_V2_PROVIDER_EMBEDDING_READ_TIMEOUT_SECONDS": "30",
        "CORTEX_V2_PROVIDER_EMBEDDING_MAX_BATCH": "64",
        "CORTEX_V2_PROVIDER_EMBEDDING_MAX_INPUT_CHARS": "65536",
        "CORTEX_V2_PROVIDER_EMBEDDING_EGRESS_ALLOWLIST": "api.embedding.test",
        "CORTEX_V2_PROVIDER_RERANK_ENABLED": "false",
        "CORTEX_V2_PROVIDER_ANALYSIS_ENABLED": "true",
        "CORTEX_V2_PROVIDER_ANALYSIS_KIND": "ollama",
        "CORTEX_V2_PROVIDER_ANALYSIS_BASE_URL": "http://127.0.0.1:11434",
        "CORTEX_V2_PROVIDER_ANALYSIS_MODEL": "qwen3:8b",
        "CORTEX_V2_PROVIDER_ANALYSIS_CREDENTIAL_FILE": "CORTEX_V2_OLLAMA_TOKEN_FILE",
        "CORTEX_V2_PROVIDER_ANALYSIS_EGRESS_ALLOWLIST": "127.0.0.1",
        "CORTEX_V2_PROVIDER_ANALYSIS_ALLOW_PRIVATE_FOR": "127.0.0.1",
        "CORTEX_V2_OLLAMA_TOKEN_FILE": "/run/secrets/cortex-v2-ollama-token",
        "CORTEX_V2_INFERENCE_ROUTING_POLICY": "remote_allowed",
        "CORTEX_V2_INFERENCE_MODELS": MODELS_JSON,
        "CORTEX_V2_INFERENCE_MAX_RESIDENT_MODELS": "2",
        "CORTEX_V2_INFERENCE_CONCURRENCY_CPU": "4",
        "CORTEX_V2_INFERENCE_CONCURRENCY_GPU": "0",
        "CORTEX_V2_INFERENCE_BATCH_LIMIT": "16",
        "CORTEX_V2_INFERENCE_IDLE_UNLOAD_SECONDS": "300",
        "CORTEX_V2_INFERENCE_OFFLINE": "false",
        "CORTEX_V2_PARSER_MAX_INPUT_CHARS": "500000",
        "CORTEX_V2_PARSER_MAX_DEPTH": "32",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_settings_accepts_complete_valid_env():
    settings = parse_settings(valid_env())
    worker = settings.worker
    assert worker.role == "embed"
    assert worker.worker_id == "embed-worker-01"
    assert worker.database_url_file == "CORTEX_V2_DATABASE_URL_FILE"
    assert worker.principal_id_file == "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE"
    assert worker.executor_roles == ("core", "doc")
    assert set(worker.executor_roles) <= set(EXECUTOR_ROLES)
    assert worker.handler_modules == (
        "cortex_v2.processing.handlers",
        "cortex_v2.retrieval.graph",
    )
    assert worker.poll_interval_seconds == 1.5
    assert worker.lease_seconds == 120
    assert isinstance(worker.lease_seconds, int)
    assert worker.heartbeat_interval_seconds == 15.0
    assert worker.attempt_deadline_seconds == 900.0
    assert worker.max_concurrent_attempts == 4
    assert worker.thread_pool_size == 8
    assert worker.backoff_base_seconds == 2.0
    assert worker.backoff_cap_seconds == 300.0
    assert worker.queue_admission_limit == 1000
    assert worker.reap_interval_seconds == 30.0
    assert worker.blob_root == "/var/lib/cortex-v2/blobs"

    assert settings.parser_limits.max_input_chars == 500_000
    assert settings.parser_limits.max_depth == 32
    assert settings.parser_limits.max_blocks == ParserLimits().max_blocks

    assert tuple(role.role for role in settings.providers) == (
        "embedding",
        "rerank",
        "analysis",
    )
    embedding = settings.provider_for_role("embedding")
    assert embedding.enabled is True
    assert embedding.provider_kind == "openai-compatible"
    assert embedding.base_url == "https://api.embedding.test/v1"
    assert embedding.model == "embed-large@2"
    assert embedding.credential_env == "CORTEX_V2_EMBEDDING_API_KEY"
    assert embedding.credential_file is None
    assert embedding.connect_timeout_seconds == 3.0
    assert embedding.read_timeout_seconds == 30.0
    assert embedding.max_batch == 64
    assert embedding.max_input_chars == 65_536
    assert embedding.egress_allowlist == ("api.embedding.test",)
    assert embedding.allow_private_for == ()
    assert settings.provider_for_role("rerank").enabled is False
    assert settings.provider_for_role("rerank").provider_kind == "none"
    analysis = settings.provider_for_role("analysis")
    assert analysis.provider_kind == "ollama"
    assert analysis.credential_file == "CORTEX_V2_OLLAMA_TOKEN_FILE"
    assert analysis.allow_private_for == ("127.0.0.1",)

    local = settings.local
    assert local.routing_policy == "remote_allowed"
    model_ids = [model.model_id for model in local.models]
    assert model_ids == ["nomic-embed-text", "qwen3:8b"]
    assert local.models[0].dimensions == 768
    assert local.models[1].dimensions is None
    assert local.max_resident_models == 2
    assert local.concurrency_cpu == 4
    assert local.concurrency_gpu == 0
    assert local.batch_limit == 16
    assert local.idle_unload_seconds == 300
    assert isinstance(local.idle_unload_seconds, int)
    assert local.offline is False
    assert local.has_installed_model("nomic-embed-text")
    assert not local.has_installed_model("qwen3:8b")
    assert not local.has_installed_model("absent-model")


def test_provider_for_role_returns_none_for_unknown_roles():
    settings = parse_settings(valid_env())
    assert settings.provider_for_role("generation") is None


def test_role_egress_policy_carries_timeouts_and_lists():
    settings = parse_settings(valid_env())
    assert settings.provider_for_role("embedding").egress_policy() == EgressPolicy(
        allowlist=("api.embedding.test",),
        allow_private_for=(),
        connect_timeout_seconds=3.0,
        read_timeout_seconds=30.0,
    )


def test_local_only_parses_when_a_model_is_installed():
    env = valid_env()
    env["CORTEX_V2_INFERENCE_ROUTING_POLICY"] = "local_only"
    assert parse_settings(env).local.routing_policy == "local_only"


def test_blob_root_defaults_to_none():
    env = valid_env()
    del env["CORTEX_V2_WORKER_BLOB_ROOT"]
    assert parse_settings(env).worker.blob_root is None
    env["CORTEX_V2_WORKER_BLOB_ROOT"] = ""
    assert parse_settings(env).worker.blob_root is None


def test_worker_numeric_defaults_are_coherent():
    env = valid_env()
    for key in (
        "CORTEX_V2_WORKER_POLL_INTERVAL_SECONDS",
        "CORTEX_V2_WORKER_LEASE_SECONDS",
        "CORTEX_V2_WORKER_HEARTBEAT_INTERVAL_SECONDS",
        "CORTEX_V2_WORKER_ATTEMPT_DEADLINE_SECONDS",
        "CORTEX_V2_WORKER_MAX_CONCURRENT_ATTEMPTS",
        "CORTEX_V2_WORKER_THREAD_POOL_SIZE",
        "CORTEX_V2_WORKER_BACKOFF_BASE_SECONDS",
        "CORTEX_V2_WORKER_BACKOFF_CAP_SECONDS",
        "CORTEX_V2_WORKER_QUEUE_ADMISSION_LIMIT",
        "CORTEX_V2_WORKER_REAP_INTERVAL_SECONDS",
        "CORTEX_V2_WORKER_EXECUTOR_ROLES",
        "CORTEX_V2_WORKER_HANDLER_MODULES",
    ):
        del env[key]
    worker = parse_settings(env).worker
    assert 0 < worker.heartbeat_interval_seconds * 2 <= worker.lease_seconds
    assert worker.backoff_base_seconds <= worker.backoff_cap_seconds
    assert worker.max_concurrent_attempts >= 1
    assert worker.executor_roles == ("core",)
    assert worker.handler_modules == ()


def test_hash_local_role_needs_no_credential_or_base_url():
    env = valid_env()
    env.update(
        {
            "CORTEX_V2_PROVIDER_EMBEDDING_KIND": "hash-local",
            "CORTEX_V2_PROVIDER_EMBEDDING_BASE_URL": "",
            "CORTEX_V2_PROVIDER_EMBEDDING_MODEL": "",
            "CORTEX_V2_PROVIDER_EMBEDDING_CREDENTIAL_ENV": "",
            "CORTEX_V2_PROVIDER_EMBEDDING_EGRESS_ALLOWLIST": "",
        }
    )
    embedding = parse_settings(env).provider_for_role("embedding")
    assert embedding.provider_kind == "hash-local"
    assert embedding.enabled is True
    assert embedding.base_url is None
    assert embedding.model == ""
    assert embedding.credential_env is None


def test_enum_constants_are_the_documented_values():
    assert PROVIDER_KINDS == ("hash-local", "openai-compatible", "ollama", "none")
    assert ROUTING_POLICIES == ("local_only", "remote_allowed", "disabled")
    assert MODEL_STATUSES == ("declared", "installed", "activated", "retired")


# ---------------------------------------------------------------------------
# Fail-closed validation (R24): every failure names the setting
# ---------------------------------------------------------------------------


def _missing(entry, field_name):
    manifest = {
        "model_id": "m",
        "digest": "sha256:d",
        "license": "apache-2.0",
        "platform": "linux/arm64",
        "status": "installed",
    }
    del manifest[field_name]
    return {entry: json.dumps([manifest])}


DECLARED_ONLY = json.dumps(
    [
        {
            "model_id": "m",
            "digest": "sha256:d",
            "license": "l",
            "platform": "p",
            "status": "declared",
        }
    ]
)

M_MODELS = "CORTEX_V2_INFERENCE_MODELS"
M_POLICY = "CORTEX_V2_INFERENCE_ROUTING_POLICY"
E = "CORTEX_V2_PROVIDER_EMBEDDING_"
A = "CORTEX_V2_PROVIDER_ANALYSIS_"
W = "CORTEX_V2_WORKER_"

INVALID_CASES: list[tuple[dict[str, str], str]] = [
    # worker
    ({W + "ROLE": "vector"}, W + "ROLE"),
    ({W + "ID": ""}, W + "ID"),
    ({W + "ID": "bad id!"}, W + "ID"),
    ({W + "POLL_INTERVAL_SECONDS": "0"}, W + "POLL_INTERVAL_SECONDS"),
    ({W + "POLL_INTERVAL_SECONDS": "-2"}, W + "POLL_INTERVAL_SECONDS"),
    ({W + "POLL_INTERVAL_SECONDS": "1e9"}, W + "POLL_INTERVAL_SECONDS"),
    ({W + "LEASE_SECONDS": "abc"}, W + "LEASE_SECONDS"),
    ({W + "LEASE_SECONDS": "12.5"}, W + "LEASE_SECONDS"),
    ({W + "HEARTBEAT_INTERVAL_SECONDS": "90"}, W + "HEARTBEAT_INTERVAL_SECONDS"),
    ({W + "ATTEMPT_DEADLINE_SECONDS": "0"}, W + "ATTEMPT_DEADLINE_SECONDS"),
    ({W + "MAX_CONCURRENT_ATTEMPTS": "0"}, W + "MAX_CONCURRENT_ATTEMPTS"),
    ({W + "MAX_CONCURRENT_ATTEMPTS": "4096"}, W + "MAX_CONCURRENT_ATTEMPTS"),
    ({W + "THREAD_POOL_SIZE": "0"}, W + "THREAD_POOL_SIZE"),
    ({W + "BACKOFF_CAP_SECONDS": "1"}, W + "BACKOFF_CAP_SECONDS"),
    ({W + "QUEUE_ADMISSION_LIMIT": "0"}, W + "QUEUE_ADMISSION_LIMIT"),
    ({W + "REAP_INTERVAL_SECONDS": "0"}, W + "REAP_INTERVAL_SECONDS"),
    ({W + "HANDLER_MODULES": "not a module"}, W + "HANDLER_MODULES"),
    ({W + "EXECUTOR_ROLES": "core,bogus"}, W + "EXECUTOR_ROLES"),
    (
        {W + "DATABASE_URL_FILE_ENV": "CORTEX_V2_MISSING_FILE"},
        W + "DATABASE_URL_FILE_ENV",
    ),
    # provider roles
    ({E + "KIND": "gpt-magic"}, E + "KIND"),
    ({E + "KIND": "none"}, E + "KIND"),  # enabled role without a real kind
    ({E + "ENABLED": "yes-please"}, E + "ENABLED"),
    ({E + "BASE_URL": "http://api.embedding.test/v1"}, E + "BASE_URL"),
    ({E + "BASE_URL": "https://evil.test/v1"}, E + "BASE_URL"),
    ({E + "BASE_URL": ""}, E + "BASE_URL"),
    ({E + "MODEL": ""}, E + "MODEL"),
    ({E + "CREDENTIAL_ENV": ""}, E + "CREDENTIAL_ENV"),
    ({E + "CREDENTIAL_FILE": "CORTEX_V2_X_FILE"}, "mutually exclusive"),
    ({E + "CONNECT_TIMEOUT_SECONDS": "0"}, E + "CONNECT_TIMEOUT_SECONDS"),
    ({E + "READ_TIMEOUT_SECONDS": "99999"}, E + "READ_TIMEOUT_SECONDS"),
    ({E + "MAX_BATCH": "-5"}, E + "MAX_BATCH"),
    ({E + "MAX_INPUT_CHARS": "0"}, E + "MAX_INPUT_CHARS"),
    ({E + "EGRESS_ALLOWLIST": "has space"}, E + "EGRESS_ALLOWLIST"),
    ({A + "BASE_URL": "ftp://127.0.0.1:11434"}, A + "BASE_URL"),
    ({A + "ALLOW_PRIVATE_FOR": ""}, A + "BASE_URL"),  # http needs the private allowance
    # local inference
    ({M_POLICY: "sometimes"}, M_POLICY),
    ({M_POLICY: ""}, M_POLICY),
    ({M_POLICY: "local_only", M_MODELS: "[]"}, M_POLICY),
    ({M_POLICY: "local_only", M_MODELS: DECLARED_ONLY}, M_POLICY),
    ({M_MODELS: "{not json"}, M_MODELS),
    ({M_MODELS: "{}"}, M_MODELS),
    ({M_MODELS: "[3]"}, M_MODELS),
    ({M_MODELS: json.dumps([{"model_id": "m"}])}, M_MODELS),
    (
        {
            M_MODELS: json.dumps(
                [
                    {
                        "model_id": "m",
                        "digest": "sha256:d",
                        "license": "l",
                        "platform": "p",
                        "status": "ghost",
                    }
                ]
            )
        },
        M_MODELS,
    ),
    (
        {
            M_MODELS: json.dumps(
                [
                    {
                        "model_id": "m",
                        "digest": "sha256:d",
                        "license": "l",
                        "platform": "p",
                        "status": "installed",
                        "dimensions": "abc",
                    }
                ]
            )
        },
        M_MODELS,
    ),
    (
        {
            M_MODELS: json.dumps(
                [
                    {
                        "model_id": "m",
                        "digest": "sha256:d",
                        "license": "l",
                        "platform": "p",
                        "status": "installed",
                        "dimensions": 999_999,
                    }
                ]
            )
        },
        M_MODELS,
    ),
    ({"CORTEX_V2_INFERENCE_MAX_RESIDENT_MODELS": "0"}, "MAX_RESIDENT_MODELS"),
    ({"CORTEX_V2_INFERENCE_CONCURRENCY_CPU": "0"}, "CONCURRENCY_CPU"),
    ({"CORTEX_V2_INFERENCE_CONCURRENCY_GPU": "-1"}, "CONCURRENCY_GPU"),
    ({"CORTEX_V2_INFERENCE_BATCH_LIMIT": "99999999"}, "BATCH_LIMIT"),
    ({"CORTEX_V2_INFERENCE_IDLE_UNLOAD_SECONDS": "0"}, "IDLE_UNLOAD_SECONDS"),
    ({"CORTEX_V2_INFERENCE_OFFLINE": "maybe"}, "OFFLINE"),
    # parser limits
    ({"CORTEX_V2_PARSER_MAX_DEPTH": "0"}, "CORTEX_V2_PARSER_MAX_DEPTH"),
    ({"CORTEX_V2_PARSER_MAX_DEPTH": "100000"}, "CORTEX_V2_PARSER_MAX_DEPTH"),
    ({"CORTEX_V2_PARSER_MAX_BLOCK_CHARS": "x"}, "CORTEX_V2_PARSER_MAX_BLOCK_CHARS"),
]


@pytest.mark.parametrize("mutation,expected_fragment", INVALID_CASES)
def test_parse_settings_fails_closed(mutation, expected_fragment):
    env = valid_env()
    env.update(mutation)
    with pytest.raises(ConfigurationError) as excinfo:
        parse_settings(env)
    assert expected_fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "field_name", ["model_id", "digest", "license", "platform", "status"]
)
def test_missing_manifest_fields_name_the_setting(field_name):
    env = valid_env()
    env.update(_missing("CORTEX_V2_INFERENCE_MODELS", field_name))
    with pytest.raises(ConfigurationError) as excinfo:
        parse_settings(env)
    message = str(excinfo.value)
    assert field_name in message
    assert "CORTEX_V2_INFERENCE_MODELS" in message


def test_duplicate_manifest_model_ids_are_rejected():
    entry = {
        "model_id": "m",
        "digest": "sha256:d",
        "license": "l",
        "platform": "p",
        "status": "installed",
    }
    env = valid_env()
    env["CORTEX_V2_INFERENCE_MODELS"] = json.dumps([entry, dict(entry)])
    with pytest.raises(ConfigurationError, match="duplicate"):
        parse_settings(env)


def test_hash_local_role_must_not_reference_credentials():
    env = valid_env()
    env.update(
        {
            "CORTEX_V2_PROVIDER_EMBEDDING_KIND": "hash-local",
            "CORTEX_V2_PROVIDER_EMBEDDING_BASE_URL": "",
            "CORTEX_V2_PROVIDER_EMBEDDING_MODEL": "",
            "CORTEX_V2_PROVIDER_EMBEDDING_EGRESS_ALLOWLIST": "",
        }
    )
    # CREDENTIAL_ENV stays set from the valid fixture: hash-local is offline.
    with pytest.raises(ConfigurationError, match="CREDENTIAL"):
        parse_settings(env)


def test_unconfigured_kind_must_not_reference_credentials():
    env = valid_env()
    env["CORTEX_V2_PROVIDER_RERANK_CREDENTIAL_ENV"] = "CORTEX_V2_RERANK_KEY"
    with pytest.raises(ConfigurationError, match="CREDENTIAL"):
        parse_settings(env)


def test_base_url_with_embedded_credentials_is_rejected_without_echo():
    env = valid_env()
    env[E + "BASE_URL"] = "https://user:hunter2-secret@api.embedding.test/v1"
    with pytest.raises(ConfigurationError) as excinfo:
        parse_settings(env)
    message = str(excinfo.value)
    assert E + "BASE_URL" in message
    assert "hunter2-secret" not in message


def test_configuration_errors_never_echo_secret_values():
    env = valid_env()
    env["CORTEX_V2_EMBEDDING_API_KEY"] = "sk-live-do-not-leak"
    env[E + "MAX_BATCH"] = "-5"
    with pytest.raises(ConfigurationError) as excinfo:
        parse_settings(env)
    assert "sk-live-do-not-leak" not in str(excinfo.value)
    assert E + "MAX_BATCH" in str(excinfo.value)


def test_repr_never_contains_secret_values():
    env = valid_env()
    env["CORTEX_V2_EMBEDDING_API_KEY"] = "sk-live-do-not-leak"
    settings = parse_settings(env)
    assert "sk-live-do-not-leak" not in repr(settings)
    assert "sk-live-do-not-leak" not in repr(settings.provider_for_role("embedding"))


def test_parse_settings_is_pure_over_the_given_mapping(monkeypatch):
    monkeypatch.setenv("CORTEX_V2_WORKER_ROLE", "doc")
    settings = parse_settings(valid_env())
    assert settings.worker.role == "embed"


def test_parse_settings_succeeds_without_secret_values_present():
    env = valid_env()
    assert "CORTEX_V2_EMBEDDING_API_KEY" not in env  # referenced, never read
    assert parse_settings(env).provider_for_role("embedding").credential_env == (
        "CORTEX_V2_EMBEDDING_API_KEY"
    )


# ---------------------------------------------------------------------------
# from_env: boot-time secret-file resolution
# ---------------------------------------------------------------------------


def test_from_env_resolves_secret_files_without_retaining_values(monkeypatch):
    for key, value in valid_env().items():
        monkeypatch.setenv(key, value)
    resolved: list[str] = []

    def fake_read_secret_path(name, *, minimum_bytes=1):
        resolved.append(name)
        return b"resolved-secret-bytes"

    monkeypatch.setattr(config, "read_secret_path", fake_read_secret_path)
    settings = ProcessingSettings.from_env()
    assert resolved == [
        "CORTEX_V2_DATABASE_URL_FILE",
        "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE",
        "CORTEX_V2_OLLAMA_TOKEN_FILE",
    ]
    assert "resolved-secret-bytes" not in repr(settings)
    assert settings.worker.role == "embed"


def test_from_env_fails_closed_when_a_secret_file_is_unavailable(monkeypatch):
    for key, value in valid_env().items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError) as excinfo:
        ProcessingSettings.from_env()
    assert "CORTEX_V2_DATABASE_URL_FILE" in str(excinfo.value)
