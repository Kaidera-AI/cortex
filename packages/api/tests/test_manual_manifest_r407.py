"""CM-1 external release metadata; real fixture bytes, no build/sign/runtime."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "packages/deploy/manual_manifest.py"
PODMAN_POLICY = json.loads((Path(__file__).parent / "fixtures/r423-podman-policy.json").read_text())
SOURCE = "ee265573e22c882d1192d6a81991fb8a5cf94187"
ROLES = ("api", "db", "tls", "provider", "embed-worker", "graph-worker", "pdf-worker")


def required():
    if not GENERATOR.exists():
        pytest.fail("R407 manual release manifest generator missing")
    spec = importlib.util.spec_from_file_location("r407_manual_manifest", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(tmp_path):
    roles = {role: {"repository": "ghcr.io/kaidera-ai/cortex-" + role,
                   "tag": "v0.1.003-manual.1", "manifest_digest": "sha256:" + str(i + 1) * 64}
             for i, role in enumerate(ROLES)}
    inventory = {"schema": "cortex.images.v1", "version": "0.1.003-manual.1",
                 "source_revision": SOURCE, "platforms": {"linux/amd64": roles}}
    artifact = tmp_path / "cortex-linux-amd64.tar.gz"
    artifact.write_bytes(b"PUBLIC synthetic prebuilt archive fixture\n")
    return inventory, {artifact.name: artifact}


def test_exact_manual_identity_role_mapping_and_measured_artifact_bytes(tmp_path):
    module = required()
    inventory, artifacts = seed(tmp_path)
    original = copy.deepcopy(inventory)
    result = module.build_manifest(source_revision=SOURCE, image_inventory=inventory, artifacts=artifacts, podman_tested_version="6.1.3")
    assert {k: result[k] for k in ("schema", "release_id", "release_lineage", "release_sequence", "api_contract", "source_revision")} == {
        "schema": "cortex.release.v1", "release_id": "v0.1.003-manual.1",
        "release_lineage": "cortex-v1-manual", "release_sequence": 1,
        "api_contract": "cortex-kos-v02009.v1", "source_revision": SOURCE}
    expected = {cm1: inventory["platforms"]["linux/amd64"][role]["repository"] + "@" +
                inventory["platforms"]["linux/amd64"][role]["manifest_digest"]
                for cm1, role in {"db": "db", "migrate": "api", "api": "api", "graph": "graph-worker",
                                  "embed": "embed-worker", "pdf": "pdf-worker"}.items()}
    assert result["images"] == {"linux/amd64": expected}
    assert result["images"]["linux/amd64"]["migrate"] == result["images"]["linux/amd64"]["api"]
    assert result["oci_inventory"] == inventory and inventory == original
    assert result["podman"] == PODMAN_POLICY
    artifact = next(iter(artifacts.values()))
    assert result["artifacts"] == {artifact.name: {"size": len(artifact.read_bytes()),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}}


@pytest.mark.parametrize("damage", ("wrong-source", "v2-version", "missing-role", "extra-role",
    "wrong-registry", "mutable-digest", "wrong-tag", "arm64-only", "artifact-missing", "artifact-link",
    "artifact-path-name", "manifest-cycle", "signature-cycle", "empty-artifacts"))
def test_invalid_inventory_or_artifact_never_becomes_signing_input(tmp_path, damage):
    module = required()
    inventory, artifacts = seed(tmp_path)
    roles = inventory["platforms"]["linux/amd64"]
    artifact = next(iter(artifacts.values()))
    if damage == "wrong-source": inventory["source_revision"] = "a" * 40
    elif damage == "v2-version": inventory["version"] = "0.2.001"
    elif damage == "missing-role": del roles["tls"]
    elif damage == "extra-role": roles["unknown"] = dict(roles["api"])
    elif damage == "wrong-registry": roles["api"]["repository"] = "other.invalid/cortex-api"
    elif damage == "mutable-digest": roles["api"]["manifest_digest"] = "latest"
    elif damage == "wrong-tag": roles["api"]["tag"] = "latest"
    elif damage == "arm64-only": inventory["platforms"] = {"linux/arm64": roles}
    elif damage == "artifact-missing": artifact.unlink()
    elif damage == "artifact-link":
        actual = artifact.with_name("PUBLIC-actual-archive")
        artifact.rename(actual)
        artifact.symlink_to(actual)
    elif damage == "artifact-path-name": artifacts = {"../outside": artifact}
    elif damage == "manifest-cycle": artifacts = {"release.json": artifact}
    elif damage == "signature-cycle": artifacts = {"release.json.minisig": artifact}
    elif damage == "empty-artifacts": artifacts = {}
    with pytest.raises(module.ManifestRefusal):
        module.build_manifest(source_revision=SOURCE, image_inventory=inventory, artifacts=artifacts, podman_tested_version="6.1.3")
