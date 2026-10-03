"""R26 argv contract: credential file references and external Podman secrets.

Provider integration is opt-in, on its installed tool runtime. The real
container_to_args environment/secret conversion is used; networking and mount
engine side effects are disabled. Its arguments go to an inert host child and
ps reads only that child's PID. No container, secret store or real key is used.

CORTEX_V2_ARGV_NEGATIVE_CONTROL=inline or env_file injects an unsafe synthetic
request and must make the exact canary assertion RED. This is a calibration of
the known provider exposure, not a claim that current v2 manifests leak.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / "deploy").glob("compose*.yaml"))
SENSITIVE = re.compile(r"PASSWORD|TOKEN|PEPPER|CREDENTIAL|DATABASE_URL|RECOVERY")


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_compose_uses_file_references_and_external_secrets(manifest):
    config = yaml.safe_load(manifest.read_text())
    for name, service in config["services"].items():
        assert not service.get("env_file"), "Compose env_file expands values into argv"
        environment = service.get("environment", {})
        assert isinstance(environment, dict)
        for key, value in environment.items():
            if SENSITIVE.search(key):
                assert key.endswith("_FILE"), name + ": raw credential environment"
                assert isinstance(value, str) and value.startswith("/run/secrets/")
        for mount in service.get("secrets", []):
            source = mount if isinstance(mount, str) else mount["source"]
            assert config["secrets"][source].get("external") is True


@pytest.mark.skipif(not os.getenv("CORTEX_V2_ARGV_PROVIDER_SOURCE"),
                    reason="explicit installed Compose provider source required")
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_provider_generated_arguments_hide_canary_in_process_list(monkeypatch, tmp_path, manifest):
    provider_path = Path(os.environ["CORTEX_V2_ARGV_PROVIDER_SOURCE"])
    spec = importlib.util.spec_from_file_location("argv_contract_provider", provider_path)
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    assert provider.__version__ == "1.5.0", "requalify a different provider version"

    async def no_engine(*args, **kwargs):
        return None
    async def metadata_mount(*args, **kwargs):
        return []
    monkeypatch.setattr(provider, "assert_cnt_nets", no_engine)
    monkeypatch.setattr(provider, "get_net_args", lambda *args: [])
    monkeypatch.setattr(provider, "get_mount_args", metadata_mount)
    config = yaml.safe_load(manifest.read_text())
    canary = "CX_ARGV_INERT_" + secrets.token_hex(16)
    mode = os.getenv("CORTEX_V2_ARGV_NEGATIVE_CONTROL")
    fake_environment = {key: canary for key in
                        ("POSTGRES_PASSWORD", "CORTEX_V2_DATABASE_URL", "CORTEX_V2_TOKEN_PEPPER")}
    compose = SimpleNamespace(dirname=str(manifest.parent), declared_secrets=config["secrets"],
                              environ=fake_environment, container_names_by_service={})
    for name, original in config["services"].items():
        service = copy.deepcopy(original)
        service.update(name="cx-argv-inert-" + name, _service=name, _deps=[])
        if mode == "inline":
            service["environment"]["CORTEX_V2_TOKEN_PEPPER"] = canary
        elif mode == "env_file":
            path = tmp_path / "inert-canary.env"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write("CORTEX_V2_TOKEN_PEPPER=" + canary + "\n")
            service["env_file"] = str(path)
        args = asyncio.run(provider.container_to_args(compose, service, no_deps=True))
        # Exact generated args, held by a harmless process for a real ps read.
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()", *args],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            snapshot = subprocess.run(["ps", "-ww", "-p", str(child.pid), "-o", "command="],
                                      capture_output=True, text=True, check=True).stdout
            assert snapshot.strip(), "ps returned no child command line"
            if canary in snapshot:
                pytest.fail("synthetic credential canary appeared in provider-generated process argv", pytrace=False)
        finally:
            child.stdin.close()
            child.wait(timeout=5)
