"""Resolve actual build COPY inputs and execute the real API probe recipe.

Filesystem materialization is source-only: native OCI/layer/permission receipts
remain required. No engine, live transport or credential is used here.
"""
import ast
import fnmatch
from pathlib import Path
import shlex
from types import SimpleNamespace
import yaml

ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ROOT / "packages"


def compose(): return yaml.safe_load((PACKAGES / "deploy/docker-compose.yml").read_text())


def copies():
    text = (PACKAGES / "api/Dockerfile").read_text().replace("\\\n", " ")
    return [shlex.split(line)[1:] for line in text.splitlines() if line.startswith("COPY ")]


def allowed(relative, ignore):
    admitted = True
    for raw in ignore.splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"): continue
        negated = rule.startswith("!")
        rule = rule.lstrip("!").rstrip("/")
        if (fnmatch.fnmatch(relative, rule) or relative == rule or relative.startswith(rule + "/")
                or any(fnmatch.fnmatch(part, rule) for part in relative.split("/"))):
            admitted = negated
    return admitted


def test_actual_copy_recipe_contains_all_canonical_migration_bytes(tmp_path):
    cfg = compose()
    build = cfg["services"]["cortex-migrate"]["build"]
    context = (PACKAGES / "deploy" / build["context"]).resolve()
    assert context == PACKAGES, "r414 API build needs the canonical schema in its context"
    assert build["dockerfile"] == "api/Dockerfile"
    seen = {}
    for instruction in copies():
        *sources, destination = instruction
        for source in sources:
            path = context / source
            assert path.exists() and context in path.resolve().parents
            for file in path.rglob("*") if path.is_dir() else [path]:
                if not file.is_file(): continue
                relative = file.relative_to(context).as_posix()
                for ignore_name in (".dockerignore", ".containerignore"):
                    assert allowed(relative, (context / ignore_name).read_text()), relative
                target = (Path(destination) / file.relative_to(path)) if path.is_dir() else Path(destination) / file.name
                if target.is_absolute() and str(target).startswith("/app/migrations/"):
                    output = tmp_path / target.relative_to("/app")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(file.read_bytes())
                    seen[file.name] = output.read_bytes()
    expected = {p.name: p.read_bytes() for p in (PACKAGES / "schema/migrations").glob("*.sql")}
    assert expected and seen == expected, "r414 image COPY must retain every canonical migration byte"


def test_api_and_finite_migrator_never_bind_source_migrations():
    cfg = compose()
    for name in ("cortex-api", "cortex-migrate"):
        service = cfg["services"][name]
        assert service["environment"]["CORTEX_MIGRATIONS_DIR"] == "/app/migrations"
        assert not any("/app/migrations" in v or "schema/migrations" in v for v in service["volumes"])
    assert cfg["services"]["cortex-api"]["image"] == cfg["services"]["cortex-migrate"]["image"]


def test_canonical_api_probe_executes_only_public_process_liveness(monkeypatch):
    import urllib.request
    calls = []
    def open_url(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(read=lambda: b'{"alive":true}')
    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    command = compose()["services"]["cortex-api"]["healthcheck"]["test"]
    assert command[:3] == ["CMD", "python", "-c"]
    exec(compile(command[3], "canonical-probe", "exec"), {})
    assert calls == [("http://localhost:8501/health/live", {"timeout": 3})]
    from service_auth_policy import PUBLIC_ROUTES, ROUTE_POLICIES
    assert PUBLIC_ROUTES == {("GET", "/health/live")}
    assert ROUTE_POLICIES[("GET", "/health")] == {"runtime:read"}
    tree = ast.parse((PACKAGES / "api/main.py").read_text())
    live = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "health_live")
    assert not any(isinstance(n, ast.Await) for n in ast.walk(live)), "public liveness must not probe database readiness"


def test_package_context_excludes_secrets_and_unshipped_host_state():
    for name in (".dockerignore", ".containerignore"):
        rules = (PACKAGES / name).read_text()
        for path in ("api/.env", "api/.env.local", "api/private.key", "api/test.pem",
                     "api/tests/test_fixture.py", "api/__pycache__/main.pyc", "api/venv/bin/python"):
            assert not allowed(path, rules), path
