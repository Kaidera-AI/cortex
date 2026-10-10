"""Run only the C11a synthetic PG gateway test in one owned Podman container."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d"


def command(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {args[0]} {args[1] if len(args)>1 else ''}")
    return result


def preflight():
    pressure = command("memory_pressure", "-Q").stdout
    match = re.search(r"System-wide memory free percentage: (\d+)%", pressure)
    if match is None or int(match.group(1)) < 35:
        raise RuntimeError("C11a Mac free-memory gate is below 35%")
    machines = json.loads(command("podman", "machine", "list", "--format", "json").stdout)
    if not any(row.get("Name") == "kos-e020-uat" and row.get("Running") for row in machines):
        raise RuntimeError("existing local Podman machine is not running")
    memory = command("podman", "machine", "ssh", "kos-e020-uat", "--", "free", "-m").stdout
    line = next((line for line in memory.splitlines() if line.startswith("Mem:")), None)
    if line is None or int(line.split()[6]) * 100 < int(line.split()[1]) * 35:
        raise RuntimeError("Podman machine available memory is below 35%")
    if command("podman", "image", "exists", IMAGE, check=False).returncode:
        raise RuntimeError("pinned PG image is not cached; no registry pull admitted")
    return int(match.group(1))


def main():
    free_percent = preflight()
    token = uuid4().hex[:10]
    name = "kaidera-test-cortex-c11a-" + token
    label = "cox-c11a-" + token
    try:
        command("podman", "run", "-d", "--pull=never", "--name", name,
                         "--label", "cortex.test=" + label, "--label", "cortex.worker=cox",
                         "--cpus", "2", "--memory", "1g", "--pids-limit", "128",
                         "--user", "postgres", "--cap-drop", "ALL",
                         "--security-opt", "no-new-privileges",
                         "--tmpfs", "/var/lib/postgresql:rw,size=512m,mode=1777",
                         "--tmpfs", "/var/run/postgresql:rw,size=16m,mode=1777",
                         "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                         "-e", "POSTGRES_DB=c11a_test", "-p", "127.0.0.1::5432", IMAGE)
        for _ in range(60):
            ready = command("podman", "exec", name, "pg_isready", "-h", "127.0.0.1",
                            "-U", "postgres", "-d", "c11a_test", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        port = command("podman", "port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        env = os.environ.copy()
        env["SEARCH_TEST_DSN"] = f"postgresql://postgres@127.0.0.1:{port}/c11a_test"
        overlay = env.get("C11A_TEST_OVERLAY")
        if overlay and (not Path(overlay).is_absolute()
                        or not (Path(overlay) / "cortex_core/api/c11a.py").is_file()):
            raise RuntimeError("C11a test overlay is invalid")
        source_paths = [str(ROOT / "src"), str(ROOT / "tests/integration")]
        if overlay:
            source_paths.insert(0, overlay)
        env["PYTHONPATH"] = os.pathsep.join(source_paths)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="cox-c11a-selection-") as selection:
            folder = ROOT / "tests/integration"
            Path(selection, "test_selection.py").write_text(
                "def load_tests(loader, tests, pattern):\n"
                f"    return loader.discover({str(folder)!r}, pattern='test_c11a_pg.py', "
                f"top_level_dir={str(folder)!r})\n")
            result = subprocess.run([sys.executable, str(ROOT / "tests/test_receipts.py"), selection],
                                    env=env, capture_output=True, text=True)
        print(json.dumps({"image": IMAGE, "container": name, "mac_free_percent": free_percent,
                          "cpus": 2, "memory_gib": 1, "exit_code": result.returncode,
                          "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True))
        return result.returncode
    finally:
        command("podman", "rm", "-f", "-v", name, check=False)
        remaining = command("podman", "ps", "-a", "--filter", "label=cortex.test=" + label,
                            "--format", "{{.Names}}").stdout.strip()
        if remaining:
            raise RuntimeError("owned C11a container cleanup failed")
        print(json.dumps({"cleanup": "pass", "owned_container": name}, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
