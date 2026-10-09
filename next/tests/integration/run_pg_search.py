"""Run synthetic integration checks in ONE disposable, resource-bounded PG container."""

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

IMAGE = "docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d"
ROOT = Path(__file__).resolve().parents[2]


def main(pattern="test_pg_search.py"):
    label = "nemo-pg-search-" + uuid.uuid4().hex[:10]
    name = "kaidera-test-pg-search-1-" + uuid.uuid4().hex[:8]
    command = [
        "podman",
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "cortex.test=" + label,
        "--label",
        "cortex.worker=nemo",
        "--cpus",
        "2",
        "--memory",
        "1g",
        "--pids-limit",
        "128",
        "--user",
        "postgres",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/var/lib/postgresql:rw,size=512m,mode=1777",
        "--tmpfs",
        "/var/run/postgresql:rw,size=16m,mode=1777",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        "POSTGRES_DB=search_test",
        "-p",
        "127.0.0.1::5432",
        IMAGE,
    ]
    try:
        started = subprocess.run(command, capture_output=True, text=True)
        if started.returncode:
            raise RuntimeError(started.stderr)
        for attempt in range(60):
            ready = subprocess.run(
                [
                    "podman",
                    "exec",
                    name,
                    "pg_isready",
                    "-U",
                    "postgres",
                    "-d",
                    "search_test",
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            print(
                subprocess.check_output(
                    ["podman", "logs", name], stderr=subprocess.STDOUT, text=True
                ),
                flush=True,
            )
            raise RuntimeError("Disposable Postgres did not become ready")
        port = (
            subprocess.check_output(["podman", "port", name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        env = dict(
            os.environ,
            SEARCH_TEST_DSN=f"postgresql://postgres@127.0.0.1:{port}/search_test",
        )
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        print(
            "Synthetic disposable PG:",
            IMAGE,
            "2 CPUs / 1 GiB; Python",
            sys.version.split()[0],
            flush=True,
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(ROOT / "tests/integration"),
                "-p",
                pattern,
                "-v",
            ],
            env=env,
        )
        return result.returncode
    finally:
        removed = subprocess.run(
            ["podman", "rm", "-f", "-v", name], capture_output=True, text=True
        )
        remaining = subprocess.check_output(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                "label=cortex.test=" + label,
                "--format",
                "{{.Names}}",
            ],
            text=True,
        )
        print(
            "cleanup:",
            "PASS" if removed.returncode == 0 and not remaining.strip() else "FAIL",
            flush=True,
        )
        if remaining.strip():
            raise RuntimeError("Disposable container remained after cleanup")


if __name__ == "__main__":
    raise SystemExit(main())
