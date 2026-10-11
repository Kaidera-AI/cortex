"""Disposable hosted-rootless PG contract. Own labels, one bounded pod, finally cleanup.

CI maintainer owns monthly digest/dependency refreshes through the same native gates.
The full Python variant is CI-only: glibc wheels, real curl and finite PKI openssl.
No engine/socket/credential-bearing checkout is mounted in the test driver.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid

import podman_stage0

PYTHON = "docker.io/library/python:3.14.7-trixie@sha256:608322fe51a9b889eee5ed6015c854e77dc44d00c116435f56b69bf6f14c88f4"
POSTGRES = "docker.io/pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a"
PAUSE = "registry.k8s.io/pause:3.10@sha256:7c38f24774e3cbd906d2d33c38354ccf787635581c122965132c9bd309754d4a"
LABEL = "cortex.pytest-pg"


class Fixture:
    def __init__(self, output, site, source):
        self.output, self.site, self.source = output, site, source
        self.run_id = os.environ.get("GITHUB_RUN_ID", "local") + "-" + uuid.uuid4().hex[:10]
        self.prefix = "kaidera-test-pg-" + self.run_id
        self.containers, self.volumes, self.secret_names = [], [], []
        self.pod = None
        self.secret_values = [secrets.token_hex(32), secrets.token_hex(32)]
        self.receipt = {"run_id": self.run_id, "images": {"python": PYTHON, "postgres": POSTGRES, "infra": PAUSE},
                        "admissions": [], "commands": [], "cgroups": [], "passes": []}

    def command(self, args, *, timeout=120, check=True, input=None):
        result = subprocess.run(["podman", *args], text=True, capture_output=True,
                                timeout=timeout, input=input)
        for secret in self.secret_values:
            result.stdout = result.stdout.replace(secret, "[REDACTED]")
            result.stderr = result.stderr.replace(secret, "[REDACTED]")
        # Secret bodies are stdin only; inspect Config.Env is never retained.
        if args[0] != "inspect":
            self.receipt["commands"].append({"args": args, "exit": result.returncode,
                                            "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise RuntimeError("podman " + " ".join(args[:3]) + ": " + result.stderr)
        return result

    def admission(self):
        fields = {line.split(":")[0]: int(line.split()[1])
                  for line in Path("/proc/meminfo").read_text().splitlines()}
        available = fields["MemAvailable"] * 100 / fields["MemTotal"]
        self.receipt["admissions"].append({"available_percent": available})
        if available < 35:
            raise RuntimeError("refused: less than35percent available memory")

    def labels(self):
        return ["--label", LABEL + "=" + self.run_id, "--label", "cortex.worker=ren-cx"]

    def owned(self, kind, name):
        args = ["inspect", name] if kind == "container" else [kind, "inspect", name]
        inspected = json.loads(self.command(args).stdout)
        row = inspected[0] if isinstance(inspected, list) else inspected
        labels = row.get("Labels", row.get("Spec", {}).get("Labels", row.get("Config", {}).get("Labels", {})))
        if labels.get(LABEL) != self.run_id:
            raise RuntimeError("ownership mismatch: " + kind + " " + name)
        return row

    def volume(self, suffix, uid=0):
        self.admission()
        name = self.prefix + "-" + suffix
        self.command(["volume", "create", *self.labels(), name])
        self.volumes.append(name)
        row = self.owned("volume", name)
        mount = row["Mountpoint"]
        self.command(["unshare", "chown", str(uid) + ":" + str(uid), mount])
        self.command(["unshare", "chmod", "0755", mount])
        return name

    def mount(self, source, target, readonly=True):
        return ["-v", str(source) + ":" + target + (":ro" if readonly else ":rw")]

    def run(self, suffix, image, command, *, user="10001:10001", memory="768m", cpus="1.5",
            mounts=(), env=None, extra=(), finite=False):
        self.admission()
        name = self.prefix + "-" + suffix
        args = ["run", "--name", name, "--pod", self.pod, "--pull=never", "--user", user,
                "--read-only", "--memory", memory, "--memory-swap", memory, "--cpus", cpus,
                "--pids-limit=256", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--tmpfs=/tmp:rw,size=64m,mode=1777", *self.labels()]
        # 4.9 forbids per-container --add-host in a pod and does not share the
        # infra /etc/hosts here. Bind only this fixture's public loopback map.
        args += self.mount(self.output / "fixture-hosts.conf", "/etc/hosts", True)
        if not finite:
            args.append("-d")
        for origin, target, readonly in mounts:
            args += self.mount(origin, target, readonly)
        for key, value in (env or {}).items():
            args += ["-e", key + "=" + value]
        args += [*extra, image, *command]
        self.containers.append(name)  # finally also handles partially-created containers
        result = self.command(args, check=False, timeout=180)
        if self.command(["container", "exists", name], check=False).returncode == 0:
            row = self.owned("container", name)
            if not finite and result.returncode == 0:
                self.cgroup(row, memory, cpus)
        if result.returncode:
            raise RuntimeError(suffix + ": " + result.stderr + result.stdout)
        return name

    def cgroup(self, row, memory, cpus):
        pid = row["State"]["Pid"]
        path = next(line.split("::", 1)[1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        leaf = Path("/sys/fs/cgroup") / path.lstrip("/")
        pod = self.owned("pod", self.pod)
        parent = Path("/sys/fs/cgroup") / pod["CgroupPath"].lstrip("/")
        assert leaf.is_relative_to(parent), (leaf, parent)
        values = {"leaf": {key: (leaf / key).read_text().strip() for key in ["memory.max", "memory.swap.max", "cpu.max"]},
                  "parent": {key: (parent / key).read_text().strip() for key in ["memory.max", "memory.swap.max", "cpu.max"]},
                  "name": row["Name"], "uid": row["Config"]["User"], "path": path,
                  "pod_path": str(parent),
                  "ports": row["NetworkSettings"].get("Ports", {})}
        expected = int(memory.removesuffix("m")) * 1024 ** 2
        assert values["leaf"]["memory.max"] == str(expected), values
        assert values["leaf"]["memory.swap.max"] == "0", values
        quota, period = map(int, values["leaf"]["cpu.max"].split())
        assert quota / period == float(cpus), values
        assert values["parent"]["memory.max"] == "1073741824", values
        assert values["parent"]["memory.swap.max"] == "0", values
        quota, period = map(int, values["parent"]["cpu.max"].split())
        assert quota / period == 2, values
        assert not values["ports"], values
        self.receipt["cgroups"].append(values)

    def remove_container(self, name):
        if self.command(["container", "exists", name], check=False).returncode == 0:
            row = self.owned("container", name)
            logs = self.command(["logs", name], check=False)
            (self.output / (name + ".txt")).write_text(logs.stdout + logs.stderr)
            self.command(["rm", "-f", row["Id"]])
        self.containers.remove(name)

    def cleanup(self):
        errors = []
        for name in list(reversed(self.containers)):
            try:
                self.remove_container(name)
            except Exception as exc:
                errors.append(str(exc))
        if self.pod:
            try:
                row = self.owned("pod", self.pod)
                self.command(["pod", "rm", "-f", row["Id"]])
                self.pod = None
            except Exception as exc:
                errors.append(str(exc))
        for name in list(self.volumes):
            try:
                self.owned("volume", name)
                self.command(["volume", "rm", name])
                self.volumes.remove(name)
            except Exception as exc:
                errors.append(str(exc))
        for name in list(self.secret_names):
            try:
                self.owned("secret", name)
                self.command(["secret", "rm", name])
                self.secret_names.remove(name)
            except Exception as exc:
                errors.append(str(exc))
        inventory = {}
        for kind, args in [("containers", ["ps", "-a"]), ("pods", ["pod", "ps"]),
                           ("volumes", ["volume", "ls"]), ("secrets", ["secret", "ls"])]:
            filtering = "name=^" + self.prefix + "-" if kind == "secrets" else "label=" + LABEL + "=" + self.run_id
            result = self.command([*args, "--filter", filtering, "--quiet"], check=False)
            inventory[kind] = result.stdout.splitlines() if result.returncode == 0 else {"error": result.stderr}
        ok = not errors and all(value == [] for value in inventory.values())
        self.receipt["cleanup"] = {"ok": ok, "errors": errors, "inventory": inventory}
        return ok

    def prepare(self):
        start = time.monotonic()
        for image in [PYTHON, POSTGRES, PAUSE]:
            self.admission()
            self.command(["pull", image], timeout=240)
        self.receipt["pull_seconds"] = time.monotonic() - start

    def create(self, pass_name):
        self.admission()
        self.pod = self.prefix + "-pod"
        self.command(["pod", "create", "--name", self.pod, "--network=none", "--hostname=cortex-pg",
                      "--add-host=cortex-pg:127.0.0.1", "--memory=1g", "--memory-swap=1g", "--cpus=2",
                      "--infra-image", PAUSE, "--infra-command=/pause", *self.labels()])
        pod = self.owned("pod", self.pod)
        assert not pod["InfraConfig"]["HostNetwork"] and not pod["InfraConfig"]["PortBindings"]
        self.tls = {role: self.volume(pass_name + "-tls-" + role) for role in ["ca", "server", "app", "admin"]}
        self.deps = self.volume(pass_name + "-deps", 10001)
        results = self.output / pass_name
        results.mkdir()
        self.command(["unshare", "chown", "10001:10001", str(results)])
        self.command(["unshare", "chmod", "0755", str(results)])
        self.results = results
        tls_mounts = [(name, "/tls/" + role, False) for role, name in self.tls.items()]
        finite_limits = podman_stage0.LIMITS.replace("os.getuid()==10001 and os.getgid()==10001", "os.getuid()==0 and os.getgid()==0")
        finite_limits += "\nimport socket;assert socket.gethostbyname('cortex-pg')=='127.0.0.1'\n"
        import shlex
        helper = self.run("prepare", PYTHON, ["sh", "-c", "python -c " + shlex.quote(finite_limits) +
                          " && cp -a /wheels/. /deps/ && sh /source/packages/deploy/tls/init.sh"],
                          user="0:0", memory="1024m", cpus="2", finite=True,
                          mounts=[(self.source, "/source", True), (self.site, "/wheels", True),
                                  (self.deps, "/deps", False), *tls_mounts],
                          extra=["--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER"])
        self.remove_container(helper)
        infra = self.owned("container", pod["InfraContainerID"])
        config = json.loads(Path(infra["OCIConfigPath"]).read_text())
        infra_user = config["process"]["user"]
        self.receipt.setdefault("pod_contracts", []).append({"pod_id": pod["Id"], "infra_id": infra["Id"],
            "infra_config_user": infra["Config"]["User"], "infra_oci_user": infra_user,
            "host_network": False, "ports": {}, "labels": pod["Labels"]})
        # Podman 4.9 explicitly blanks the infra inspect Config.User; use OCI.
        assert infra_user["uid"] == infra_user["gid"] == 65535, infra_user
        self.secrets = {}
        for suffix, value in zip(["admin", "password"], self.secret_values):
            name = self.prefix + "-" + suffix
            self.command(["secret", "create", *self.labels(), name, "-"], input=value)
            self.secret_names.append(name)
            self.owned("secret", name)
            self.secrets[suffix] = name

    def database(self, suffix, port, *, cert=False, password=False, memory="256m", cpus="0.5"):
        data = self.volume(suffix + "-data", 999)
        env = {"POSTGRES_DB": "platform_agent_memory", "PGDATA": "/var/lib/postgresql/data/pgdata"}
        mounts = [(data, "/var/lib/postgresql/data", False), (self.tls["server"], "/tls/server", True)]
        extra = ["--tmpfs=/var/run/postgresql:rw,size=8m,mode=1777"]
        args = ["postgres", "-p", str(port), "-c", "shared_buffers=16MB", "-c", "max_connections=24",
                "-c", "unix_socket_directories=/var/run/postgresql", "-c", "listen_addresses=127.0.0.1"]
        if password:
            env.update(POSTGRES_PASSWORD_FILE="/run/secrets/pg-password", POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256")
            extra += ["--secret", self.secrets["password"] + ",target=pg-password,uid=999,gid=999,mode=0400"]
            args += ["-c", "ssl=on", "-c", "ssl_cert_file=/tls/server/tls.crt", "-c", "ssl_key_file=/tls/server/tls.key"]
        else:
            hba = self.source / "packages/deploy/pg_hba.conf" if cert else self.output / "fixture-hba.conf"
            mounts += [(self.source / "packages/deploy/db-entrypoint.sh", "/usr/local/bin/cortex-db-entrypoint", True),
                       (hba, "/etc/cortex/pg_hba.conf", True),
                       (self.source / "packages/deploy/pg_ident.conf", "/etc/cortex/pg_ident.conf", True),
                       (self.source / "packages/schema/initdb/00-cortex-bootstrap.sh", "/docker-entrypoint-initdb.d/00-cortex-bootstrap.sh", True),
                       (self.source / "packages/deploy/initdb/30-cortex-baseline.sh", "/docker-entrypoint-initdb.d/30-cortex-baseline.sh", True),
                       (self.source / "packages/deploy/initdb/40-cortex-local-defaults.sh", "/docker-entrypoint-initdb.d/40-cortex-local-defaults.sh", True),
                       (self.source / "packages/schema/cortex-schema-full.sql", "/cortex-bootstrap/cortex-schema-full.sql", True),
                       (self.output / "baseline.sql", "/schema-baseline/baseline.sql", True)]
            # The production image's COPY makes this source executable; a git
            # archive preserves its 0644 mode, so interpret the identical bytes.
            extra += ["--entrypoint=/bin/bash"]
            args.insert(0, "/usr/local/bin/cortex-db-entrypoint")
        name = self.run(suffix, POSTGRES, args, user="999:999", memory=memory, cpus=cpus,
                        mounts=mounts, env=env, extra=extra)
        ready = False
        for _ in range(100):
            row = self.owned("container", name)
            if not row["State"]["Running"]:
                break
            result = self.command(["exec", name, "pg_isready", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres"], check=False)
            if result.returncode == 0:
                ready = True
                break
            time.sleep(.3)
        if not ready:
            raise RuntimeError(suffix + " database readiness failed")
        return name

    def driver(self, suffix, mode):
        run = self.run_id
        admin = "postgresql://postgres@127.0.0.1:55431/platform_agent_memory"
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
               "PYTHONPATH": "/app", "CORTEX_DB_AUTH": "certificate",
               "CORTEX_PG_DSN": "postgresql://postgres@cortex-pg:55431/platform_agent_memory",
               "CORTEX_PG_DSN_ADMIN": "postgresql://postgres@cortex-pg:55431/platform_agent_memory",
               "CORTEX_PG_DSN_APP": "postgresql://cortex_app@cortex-pg:55431/platform_agent_memory",
               "CORTEX_TEST_PG_DSN": admin, "CORTEX_TEST_PG_DSN_APP": "postgresql://cortex_app@127.0.0.1:55431/platform_agent_memory",
               "CORTEX_AUDIT_PG_DSN": "postgresql://postgres@127.0.0.1:55431/postgres",
               "CORTEX_TEST_PROJECT": "ci-api-" + run, "CORTEX_CI_API_PROJECT": "ci-api-" + run,
               "CORTEX_CI_RLS_PROJECT": "ci-rls-" + run,
               "CORTEX_OWNED_RLS_DSN": admin, "CORTEX_OWNED_RLS_RUN_ID": run,
               "CORTEX_REQUIRE_OWNED_RLS_TESTS": "1", "CORTEX_REQUIRE_POSTGRES_TESTS": "1",
               "CORTEX_SERVICE_AUTH_ENABLED": "false", "CORTEX_API_URL": "http://127.0.0.1:8501",
               "CORTEX_MIGRATIONS_DIR": "/source/packages/schema/migrations",
               "CORTEX_REQUIRE_NATIVE_CERT_TESTS": "1", "CORTEX_CERT_FIXTURE_LIFECYCLE": "nemo-cert-ci-" + run,
               "CORTEX_CERT_TEST_DSN": "postgresql://postgres@cortex-pg:5432/platform_agent_memory",
               "CORTEX_PASSWORD_TEST_DSN": "postgresql://postgres@cortex-pg:5433/platform_agent_memory"}
        mounts = [(self.source, "/source", True), (self.source / "packages/api", "/app", True),
                  (self.deps, "/usr/local/lib/python3.14/site-packages", True),
                  (self.results, "/results", False), (self.tls["app"], "/tls/app", True),
                  (self.tls["admin"], "/tls/admin", True)]
        driver = self.run(suffix, PYTHON, ["sleep", "1800"], mounts=mounts, env=env,
                          extra=["--secret", self.secrets["admin"] + ",type=env,target=CORTEX_ADMIN_TOKEN",
                                 "--secret", self.secrets["password"] + ",type=env,target=PGPASSWORD"])
        result = self.command(["exec", driver, "python", "/source/.github/scripts/pytest_pg_worker.py",
                               "--mode", mode, "--output", "/results/" + mode], timeout=900, check=False)
        (self.output / (self.results.name + "-" + mode + "-driver.txt")).write_text(result.stdout + result.stderr)
        print(mode, result.stdout, result.stderr, flush=True)
        self.remove_container(driver)
        return result.returncode

    def execute(self, pass_name):
        start = time.monotonic()
        self.create(pass_name)
        pg = self.database("general-pg", 55431)
        general_exit = self.driver("general-driver", "general")
        self.remove_container(pg)
        cert = self.database("cert-pg", 5432, cert=True, memory="128m", cpus="0.25")
        password = self.database("password-pg", 5433, password=True, memory="128m", cpus="0.25")
        certificate_exit = self.driver("cert-driver", "certificate")
        self.remove_container(password)
        self.remove_container(cert)
        general = json.loads((self.results / "general/batches.json").read_text())
        positive = [batch for batch in general if not batch["control"]]
        reports = [json.loads((self.results / "general" / batch["report"]).read_text()) for batch in positive]
        reports.append(json.loads((self.results / "certificate/certificates.json").read_text()))
        nodes = [node for report in reports for node in report["selected"]]
        assert len(nodes) == len(set(nodes)) == 51, nodes
        self.receipt["passes"].append({"name": pass_name, "selected": nodes,
                                       "passed": sum(len(report["passed"]) for report in reports),
                                       "failed": [node for report in reports for node in report["failed"]],
                                       "worker_exits": {"general": general_exit, "certificate": certificate_exit},
                                       "fixture_seconds": time.monotonic() - start})
        assert general_exit == certificate_exit == 0, "native worker failed"
        assert all(len(report["passed"]) == len(report["selected"]) and not report["failed"]
                   and not report["skipped"] and not report["deselected"] for report in reports)
        if not self.cleanup():
            raise RuntimeError("owned cleanup failed")


def provision(output, site):
    start = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    source = output.parent / (output.name + "-source")
    fixture = Fixture(output, site, source)
    try:
        if podman_stage0.probe(output / "preflight"):
            raise RuntimeError("Stage0 preflight refused")
        source.mkdir()
        archive = subprocess.run(["git", "archive", "HEAD"], capture_output=True, check=True).stdout
        import io
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter="data")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        fixture.receipt["source_sha"] = sha
        fixture.receipt["source_files"] = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                                            for path in source.rglob("*") if path.is_file()}
        (output / "fixture-hba.conf").write_text("local all all peer\nhost all all 127.0.0.1/32 trust\n")
        (output / "fixture-hosts.conf").write_text("127.0.0.1 localhost cortex-pg\n::1 localhost\n")
        baseline = subprocess.run([sys.executable, str(source / "packages/deploy/baseline.py"), "--render-sql",
                                   "--schema", str(source / "packages/schema/cortex-schema-full.sql"),
                                   "--migrations", str(source / "packages/schema/migrations")], text=True, capture_output=True, check=True)
        (output / "baseline.sql").write_text(baseline.stdout)
        fixture.prepare()
        fixture.execute("cold")
        fixture.receipt["cold_seconds"] = time.monotonic() - start
        warm = time.monotonic()
        fixture.execute("warm")
        fixture.receipt["warm_seconds"] = time.monotonic() - warm
        fixture.receipt["status"] = "PASS"
    except BaseException as exc:
        fixture.receipt["status"] = "FAIL"
        fixture.receipt["reason"] = type(exc).__name__ + ": " + str(exc)
        print(fixture.receipt["reason"], flush=True)
    finally:
        if not fixture.cleanup():
            fixture.receipt["status"] = "FAIL"
        fixture.receipt["total_seconds"] = time.monotonic() - start
        fixture.command(["unshare", "chown", "-R", "0:0", str(output)])
        (output / "provisioner.json").write_text(json.dumps(fixture.receipt, indent=2) + "\n")
        # Only sanitized JSON/text leave the runner. PKI remains solely in removed volumes.
        for path in output.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".txt"}:
                value = path.read_text()
                for secret in fixture.secret_values:
                    value = value.replace(secret, "[REDACTED]")
                path.write_text(value)
        artifact_bytes = sum(path.stat().st_size for path in output.rglob("*")
                             if path.is_file() and path.suffix in {".json", ".txt"})
        fixture.receipt["artifact_bytes"] = artifact_bytes
        if artifact_bytes > 10 * 1024 ** 2:
            fixture.receipt["status"] = "FAIL"
            fixture.receipt["reason"] = "JSON/text evidence exceeds10MiB"
        (output / "provisioner.json").write_text(json.dumps(fixture.receipt, indent=2) + "\n")
        shutil.rmtree(source, ignore_errors=True)
    print(json.dumps({key: fixture.receipt.get(key) for key in ["status", "reason", "cold_seconds", "warm_seconds", "total_seconds", "cleanup"]}))
    return 0 if fixture.receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    def terminate(signum, frame):
        raise SystemExit("signal " + str(signum))
    signal.signal(signal.SIGTERM, terminate)
    args = parser.parse_args()
    raise SystemExit(provision(args.output.resolve(), args.site.resolve()))
