"""Manual Compose must supply immutable CM-1 image identity; no engine/live API."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "packages/api/release_identity.py"
IDENTITY = {
    "release_id": "v0.1.003-manual.1",
    "release_lineage": "cortex-v1-manual",
    "release_sequence": 1,
    "api_contract": "cortex-kos-v02009.v1",
    "source_revision": "13481d7f991d768e1ba11d970d9df7e8abba4dea",
}
PUBLIC_BUILD_ENV = {
    "CORTEX_VERSION": IDENTITY["release_id"],
    "CORTEX_SOURCE_REVISION": IDENTITY["source_revision"],
    "CORTEX_RELEASE_LINEAGE": IDENTITY["release_lineage"],
    "CORTEX_RELEASE_SEQUENCE": str(IDENTITY["release_sequence"]),
    "CORTEX_API_CONTRACT": IDENTITY["api_contract"],
}
FIELDS = {
    "CORTEX_RELEASE_LINEAGE": "release_lineage",
    "CORTEX_RELEASE_SEQUENCE": "release_sequence",
    "CORTEX_API_CONTRACT": "api_contract",
}


def compose_args():
    document = yaml.safe_load((ROOT / "packages/deploy/docker-compose.yml").read_text())
    args = document["services"]["cortex-migrate"]["build"]["args"]
    assert args == document["x-build-args"]
    return args


def expand(value):
    match = re.fullmatch(r"\$\{([A-Z_]+):\?[^}]+\}", value)
    return PUBLIC_BUILD_ENV[match[1]] if match else value


def generator(tmp_path):
    # The real build-time module writes beside itself, so use its exact bytes in scratch.
    path = tmp_path / "release_identity.py"
    source = GENERATOR.read_bytes()
    path.write_bytes(source)
    assert path.read_bytes() == source
    spec = importlib.util.spec_from_file_location("r403_image_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def generate(path, values):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CORTEX_", "KAIDERA_", "OPENKAI_", "HARNESS_"))
           and k not in {"DATABASE_URL", "PGPASSWORD", "PGPASSFILE"}}
    command = [sys.executable, str(path), values["release_id"], values["release_lineage"],
               str(values["release_sequence"]), values["api_contract"], values["source_revision"]]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(path.with_name("release_identity.json").read_text())


@pytest.mark.parametrize("arg,field", FIELDS.items())
def test_manual_compose_forwards_each_required_signed_identity_field(arg, field):
    args = compose_args()
    assert arg in args, "R403 manual image identity build input missing: " + arg
    actual = expand(args[arg])
    assert actual == str(IDENTITY[field])


def test_real_image_identity_generator_receives_the_release_compose_inputs(tmp_path):
    args = compose_args()
    missing = sorted(set(FIELDS) - set(args))
    assert not missing, "R403 manual image identity build inputs missing: " + ", ".join(missing)
    values = {
        "release_id": expand(args["KOS_VERSION"]),
        "source_revision": expand(args["KOS_SOURCE_REVISION"]),
        **{field: expand(args[arg]) for arg, field in FIELDS.items()},
    }
    values["release_sequence"] = int(values["release_sequence"])
    path, module = generator(tmp_path)
    assert generate(path, values) == IDENTITY
    assert module.load_release_identity() == IDENTITY


def test_baked_identity_is_exact_and_runtime_environment_cannot_override_it(tmp_path, monkeypatch):
    path, module = generator(tmp_path)
    assert generate(path, IDENTITY) == IDENTITY
    for field in PUBLIC_BUILD_ENV:
        monkeypatch.setenv(field, "PUBLIC runtime override must be ignored")
    assert module.load_release_identity() == IDENTITY


def test_missing_baked_file_stays_unqualified(tmp_path):
    _, module = generator(tmp_path)
    assert module.load_release_identity() == {}


@pytest.mark.parametrize("change", ({"release_sequence": True}, {"source_revision": "bad"},
                                  {"api_contract": ""}, {"extra": "PUBLIC"}))
def test_invalid_baked_identity_never_becomes_an_accepted_release(tmp_path, change):
    _, module = generator(tmp_path)
    with pytest.raises(RuntimeError):
        module.validate_release_identity({**IDENTITY, **change})


def test_duplicate_baked_identity_field_is_refused(tmp_path):
    path, module = generator(tmp_path)
    body = json.dumps(IDENTITY)
    path.with_name("release_identity.json").write_text(body[:-1] + ', "release_sequence": 2}')
    with pytest.raises(RuntimeError, match="Duplicate"):
        module.load_release_identity()


def test_linked_baked_identity_is_refused(tmp_path):
    path, module = generator(tmp_path)
    target = tmp_path / "PUBLIC identity target.json"
    target.write_text(json.dumps(IDENTITY))
    path.with_name("release_identity.json").symlink_to(target)
    with pytest.raises((OSError, RuntimeError)) as error:
        module.load_release_identity()
    if isinstance(error.value, OSError):
        assert error.value.errno == errno.ELOOP
