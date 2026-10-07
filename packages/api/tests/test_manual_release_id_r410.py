"""Execute the actual image identity RUN recipe without building an image."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "packages/api/Dockerfile"
RELEASE = "v0.1.003-manual.1"
VERSION = "0.1.003-manual.1"
SOURCE = "62f95cb92df23b22ffa164239873c0b52d143725"


def bake(tmp_path, release_id):
    source = (ROOT / "packages/api/release_identity.py").read_bytes()
    generator = tmp_path / "release_identity.py"
    generator.write_bytes(source)
    assert generator.read_bytes() == source
    text = DOCKERFILE.read_text()
    block = re.search(r'^RUN if \[ -n "\$CORTEX_RELEASE_SEQUENCE" \]; then .*?^    fi$', text, re.M | re.S)
    assert block is not None
    recipe = block[0][4:].replace("\\\n", " ")
    recipe = recipe.replace("python /app/release_identity.py", shlex.quote(sys.executable) + " " + shlex.quote(str(generator)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CORTEX_", "KAIDERA_", "OPENKAI_", "HARNESS_"))
           and k not in {"DATABASE_URL", "PGPASSWORD", "PGPASSFILE", "BASH_ENV", "ENV"}}
    env.update(KOS_VERSION=VERSION, KOS_SOURCE_REVISION=SOURCE, CORTEX_RELEASE_ID=release_id,
               CORTEX_RELEASE_SEQUENCE="1", CORTEX_RELEASE_LINEAGE="cortex-v1-manual",
               CORTEX_API_CONTRACT="cortex-kos-v02009.v1")
    return subprocess.run(["/bin/bash", "-c", recipe], env=env, capture_output=True, text=True, timeout=10), generator


def test_real_build_recipe_bakes_explicit_release_id_not_package_label(tmp_path):
    result, generator = bake(tmp_path, RELEASE)
    assert result.returncode == 0, result.stderr
    baked = json.loads(generator.with_name("release_identity.json").read_text())
    assert baked == {"release_id": RELEASE, "release_lineage": "cortex-v1-manual", "release_sequence": 1,
                     "api_contract": "cortex-kos-v02009.v1", "source_revision": SOURCE}
    assert baked["release_id"] != VERSION


def test_real_build_recipe_never_derives_or_falls_back_without_explicit_id(tmp_path):
    result, generator = bake(tmp_path, "")
    assert result.returncode != 0, "R410 missing explicit release ID must refuse"
    assert not generator.with_name("release_identity.json").exists()


def test_exact_release_id_is_a_declared_and_required_build_input():
    assert "ARG CORTEX_RELEASE_ID\n" in DOCKERFILE.read_text(), "R410 separate release ID ARG missing"
    compose = yaml.safe_load((ROOT / "packages/deploy/docker-compose.yml").read_text())
    args = compose["services"]["cortex-migrate"]["build"]["args"]
    assert args["CORTEX_RELEASE_ID"] == "${CORTEX_RELEASE_ID:?set CORTEX_RELEASE_ID}"
