"""Disposable synthetic SQL diagnostics. Never connects to an existing database."""
import argparse
from collections import defaultdict
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import psycopg
from psycopg.types.json import Jsonb
from . import corpus, oracle

IMAGE = "docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d"
OPERATORS = {"cosine": "<=>", "dot": "<#>", "euclidean": "<->"}
OPCLASSES = {"cosine": "vector_cosine_ops", "dot": "vector_ip_ops", "euclidean": "vector_l2_ops"}
SETTINGS = {"m": 16, "ef_construction": 64, "ef_search": 200, "iterative_scan": "strict_order",
            "max_scan_tuples": 20000, "scan_mem_multiplier": 2}


def podman(args, *, input=None):
    r = subprocess.run(["podman", *args], input=input, text=True, capture_output=True, timeout=60)
    if r.returncode:
        # No raw command/stdout/stderr: the operation may involve credentials.
        raise RuntimeError(f"Podman {args[0]} failed (exit {r.returncode})")
    return r.stdout.strip()


def require_synthetic(manifest, admission=None):
    if manifest["dataset"] != "synthetic":
        from .geometry import admit_artifact
        admit_artifact(manifest, admission)


class DisposablePostgres:
    def __init__(self, *, image=IMAGE, runner=podman):
        if image != IMAGE:
            raise ValueError("only the reviewed immutable image pin is admitted")
        self.image, self.runner = image, runner
        self.name = f"kaidera-dev-vector-baseline-{secrets.randbits(64)}"
        self.lifecycle = secrets.token_hex(16)
        self.owned, self.pending, self.binding = set(), set(), {}
        self.connection, self.lock = None, None
        self.cleanup_verified = False
        self.password = None
        self.used = False
        self.worker = "mike"
        self.preflights = []
        self.marker_path = None
        self.marker_owned = False

    def marker_identity(self):
        return {"schema": "cortex-b01-lifecycle-admission-v1", "name": self.name,
                "lifecycle": self.lifecycle, "image": self.image}

    def read_marker(self):
        try:
            data = json.loads(self.marker_path.read_text())
            valid = (isinstance(data, dict) and set(data) == {"schema", "name", "lifecycle", "image"}
                     and data["schema"] == "cortex-b01-lifecycle-admission-v1"
                     and isinstance(data["name"], str)
                     and re.fullmatch(r"kaidera-dev-vector-baseline-[0-9]{1,20}", data["name"])
                     and isinstance(data["lifecycle"], str)
                     and re.fullmatch(r"[0-9a-f]{32}", data["lifecycle"])
                     and data["image"] == IMAGE)
            if not valid:
                raise ValueError("invalid pending identity")
            return data
        except Exception:
            raise RuntimeError("pending lifecycle identity could not be established") from None

    def sync_marker_directory(self):
        descriptor = os.open(self.marker_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def acquire_admission(self, lock_path):
        self.marker_path = lock_path.with_name("b01-podman.pending.json")
        if self.marker_path.exists():
            data = self.read_marker()
            previous = object.__new__(DisposablePostgres)
            previous.runner, previous.name, previous.lifecycle = self.runner, data["name"], data["lifecycle"]
            try:
                remaining = [previous.owned_resource(kind) for kind in ("container", "volume", "secret")]
            except Exception:
                raise RuntimeError("pending lifecycle absence could not be verified") from None
            if any(remaining):
                raise RuntimeError("pending lifecycle resources still require cleanup")
            self.marker_path.unlink()
            self.sync_marker_directory()
        descriptor = os.open(self.marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as marker:
            json.dump(self.marker_identity(), marker, sort_keys=True)
            marker.flush()
            os.fsync(marker.fileno())
        self.sync_marker_directory()
        self.marker_owned = True

    def labels(self):
        labels = ["--label", "worker=" + self.worker, "--label", f"kaidera.b01.lifecycle={self.lifecycle}"]
        if self.worker == "nemo":
            labels.extend(["--label", "cortex.worker=nemo", "--label", "cortex.test=" + self.lifecycle])
        return labels

    def run_args(self):
        return ["create", "--name", self.name, *self.labels(), "--user", "999:999", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--cpus=2", "--memory=1g", "--pids-limit=256",
                "--shm-size=256m", "-p", "127.0.0.1::5432", "--volume", f"{self.name}:/var/lib/postgresql",
                "--secret", f"{self.name},type=mount,target=pgpass,uid=999,gid=999,mode=0400",
                "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/pgpass", self.image,
                "postgres", "-c", "shared_buffers=128MB", "-c", "max_connections=16"]

    def owned_resource(self, resource):
        inventory = (["ps", "-a", "--format", "{{.Names}}"] if resource == "container"
                     else [resource, "ls", "--format", "{{.Name}}"])
        if self.name not in self.runner(inventory).splitlines():
            return None
        rows = json.loads(self.runner([resource, "inspect", self.name]))
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("resource inspection did not establish identity")
        row = rows[0]
        if resource == "container":
            name, labels, target = row["Name"].removeprefix("/"), row["Config"]["Labels"], row["Id"]
        elif resource == "secret":
            name, labels, target = row["Spec"]["Name"], row["Spec"].get("Labels"), row["ID"]
        else:
            name, labels, target = row["Name"], row.get("Labels"), row["Name"]
        if name != self.name or (labels or {}).get("kaidera.b01.lifecycle") != self.lifecycle:
            return None  # A pre-existing/colliding identity never becomes ours.
        if not isinstance(target, str) or not target or any(c.isspace() for c in target):
            raise RuntimeError("resource inspection did not establish removal target")
        return target

    def create(self, resource, args, **kwargs):
        self.pending.add(resource)  # The external effect may precede any acknowledgement.
        self.runner(args, **kwargs)
        target = self.owned_resource(resource)
        if target is None:
            raise RuntimeError("create did not establish this lifecycle's ownership")
        self.owned.add(resource)
        self.pending.remove(resource)
        return target

    def __enter__(self):
        if self.used:
            raise ValueError("a disposable lifecycle is single-use; create a fresh stack")
        self.used = True
        lock_path = Path.home() / ".cache/kaidera/b01-podman.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = lock_path.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquire_admission(lock_path)
            if self.worker == "nemo":
                from .qdrant import preflight
                self.preflights.append(preflight(self.runner))
            self.password = secrets.token_urlsafe(32)
            self.create("volume", ["volume", "create", "--uid", "999", "--gid", "999", *self.labels(), self.name])
            self.create("secret", ["secret", "create", *self.labels(), self.name, "-"], input=self.password)
            container = self.create("container", self.run_args())
            if self.worker == "nemo":
                self.preflights.append(preflight(self.runner, allowed_names=(self.name,)))
            self.runner(["start", container])
            endpoint = self.runner(["port", container, "5432/tcp"])
            host, port = endpoint.split(":")
            if host != "127.0.0.1" or not port.isdigit() or int(port) in (5500, 8501):
                raise RuntimeError("unexpected endpoint; refusing connection")
            deadline = time.monotonic() + 45
            while True:
                try:
                    self.connection = psycopg.connect(host=host, port=int(port), user="postgres", dbname="postgres",
                                                       password=self.password, connect_timeout=2, autocommit=True)
                    break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("owned PostgreSQL startup timeout") from None
                    time.sleep(.25)
            self.connection.execute("CREATE EXTENSION vector")
            self.binding = {"image": self.image, "container": self.name, "lifecycle": self.lifecycle,
                            "postgres_version": self.connection.execute("SHOW server_version").fetchone()[0],
                            "pgvector_version": self.connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0],
                            "platform": json.loads(self.runner(["image", "inspect", self.image]))[0]["Architecture"],
                            "settings": SETTINGS, "limits": {"cpus": 2, "memory_bytes": 1073741824},
                            "uid": 999, "scope": "synthetic-precomputed-SQL-diagnostic",
                            "preflights": self.preflights}
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        errors = []
        self.cleanup_verified = False
        try:
            if self.marker_owned and self.lock is None:
                self.lock = self.marker_path.with_name("b01-podman.lock").open("a")
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.connection is not None:
                try:
                    self.connection.close()
                except Exception as e:
                    errors.append(type(e).__name__)
                self.connection = None
            for resource in ("container", "volume", "secret"):
                if resource in self.owned | self.pending:
                    try:
                        target = self.owned_resource(resource)
                        if target is not None:
                            args = ["rm", "-f", target] if resource == "container" else [resource, "rm", target]
                            self.runner(args)
                        self.owned.discard(resource)
                        self.pending.discard(resource)
                    except Exception as e:
                        errors.append(f"{resource}: {type(e).__name__}")
            try:
                remaining = [self.owned_resource(resource) for resource in ("container", "volume", "secret")]
                self.cleanup_verified = not errors and not self.owned and not self.pending and not any(remaining)
                if not self.cleanup_verified and not errors:
                    errors.append("owned resource remains")
            except Exception as e:
                errors.append(f"cleanup inventory: {type(e).__name__}")
            if self.cleanup_verified and self.marker_owned:
                try:
                    if self.marker_path.exists():
                        if self.read_marker() != self.marker_identity():
                            raise RuntimeError("pending lifecycle identity changed")
                        self.marker_path.unlink()
                        self.sync_marker_directory()
                    self.marker_owned = False
                except Exception as e:
                    self.cleanup_verified = False
                    errors.append(f"cleanup admission: {type(e).__name__}")
        finally:
            self.password = None
            if self.lock is not None:
                self.lock.close()
                self.lock = None
        if errors:
            raise RuntimeError("cleanup incomplete: " + "; ".join(errors))

    def __exit__(self, *exc):
        self.close()


def predicate(q):
    conditions = ["tenant = %s", "project = %s", "NOT deleted", "generation = %s"]
    params = [q["tenant"], q["project"], q["generation"]]
    for key, column, op in (("lo", "ordinal", ">="), ("hi", "ordinal", "<="), ("kind", "kind", "="),
                            ("time_lo", "occurred", ">="), ("time_hi", "occurred", "<=")):
        if q[key] is not None:
            conditions.append(f"{column} {op} %s")
            params.append(q[key])
    return " AND ".join(conditions), params


def vector_literal(v):
    return "[" + ",".join(format(float(x), ".9g") for x in v) + "]"


def dense_query(q, metric, *, limit=200):
    op = OPERATORS[metric]
    where, params = predicate(q)
    vector = vector_literal(q["vector"])
    sql = (f"WITH candidates AS MATERIALIZED (SELECT id, embedding {op} %s::vector AS distance "
           f"FROM points WHERE {where} ORDER BY embedding {op} %s::vector "
           "FETCH FIRST %s ROWS WITH TIES) SELECT id, distance FROM candidates ORDER BY distance, id LIMIT %s")
    return sql, [vector, *params, vector, limit, limit]


def install(connection, c, *, admission=None):
    require_synthetic(c.manifest, admission)
    dimension = c.identity["dimension"]
    connection.execute(f"CREATE TABLE points(id text PRIMARY KEY, tenant text NOT NULL, project text NOT NULL, "
                       f"kind integer NOT NULL, occurred bigint NOT NULL, ordinal bigint NOT NULL, deleted boolean NOT NULL, "
                       f"generation text NOT NULL, embedding vector({dimension}) NOT NULL, sparse jsonb)")
    with connection.cursor() as cursor:
        with cursor.copy("COPY points FROM STDIN") as copy:
            for i, row in enumerate(c.records):
                sparse = [[int(t), float(w)] for t, w in zip(c.indices[c.offsets[i]:c.offsets[i + 1]],
                                                           c.weights[c.offsets[i]:c.offsets[i + 1]])]
                copy.write_row((row["id"].decode(), row["tenant"].decode(), row["project"].decode(),
                                int(row["kind"]), int(row["time"]), int(row["ordinal"]), bool(row["deleted"]),
                                c.identity["generation"], vector_literal(c.vectors[i]),
                                Jsonb(sparse) if row["has_sparse"] else None))
    connection.execute("CREATE INDEX points_scope ON points(tenant, project, ordinal)")
    connection.execute(f"CREATE INDEX points_hnsw ON points USING hnsw(embedding {OPCLASSES[c.identity['metric']]}) "
                       "WITH (m=16, ef_construction=64)")
    connection.execute("ANALYZE points")


def evaluate(connection, c, q):
    require_synthetic(c.manifest)
    truth = oracle.rank(c, q)  # Full scan outside timed SQL, no candidate-defined truth.
    mode = q["mode"]
    truth.key(mode)  # Missing sparse inputs cannot become a successful empty search.
    sql, params = dense_query(q, c.identity["metric"])
    with connection.transaction():
        for name in ("ef_search", "iterative_scan", "max_scan_tuples", "scan_mem_multiplier"):
            connection.execute("SELECT set_config(%s, %s, true)", [f"hnsw.{name}", str(SETTINGS[name])])
        connection.execute("SET LOCAL statement_timeout='10s'")
        plan = "\n".join(r[0] for r in connection.execute("EXPLAIN " + sql, params))
        started = time.perf_counter()
        dense = [r[0] for r in connection.execute(sql, params)]
        sparse = []
        if mode in ("hybrid", "sparse"):
            where, filters = predicate(q)
            sparse_sql = ("SELECT id, sum((term->>1)::double precision * (queryterm->>1)::double precision) AS score "
                          "FROM points CROSS JOIN LATERAL jsonb_array_elements(sparse) term "
                          f"CROSS JOIN jsonb_array_elements(%s::jsonb) queryterm WHERE {where} "
                          "AND (term->>0)::integer = (queryterm->>0)::integer GROUP BY id "
                          "HAVING sum((term->>1)::double precision * (queryterm->>1)::double precision)>0 "
                          "ORDER BY score DESC, id LIMIT 200")
            sparse = [r[0] for r in connection.execute(sparse_sql, [Jsonb(q["sparse"]), *filters])]
        fused = defaultdict(float)
        for branch in (dense, sparse):
            for rank, sid in enumerate(branch):
                fused[sid] += 1.0 / (2.0 + rank)
        ids = (sorted(fused, key=lambda sid: (-fused[sid], sid)) if mode == "hybrid"
               else sparse if mode == "sparse" else dense)[:10]
        duration = (time.perf_counter() - started) * 1000
        # An index-only witness is labelled separately from the natural filtered plan.
        connection.execute("SET LOCAL enable_seqscan=off")
        witness = connection.execute(f"EXPLAIN SELECT id FROM points ORDER BY embedding {OPERATORS[c.identity['metric']]} "
                                     "%s::vector LIMIT 10", [vector_literal(q["vector"])])
        forced = "\n".join(r[0] for r in witness)
    measurement = oracle.measure(truth, ids, mode)
    finite = oracle.measure(truth, ids, mode, full=False) if mode == "hybrid" else None
    ordered = truth.orders["dense"]
    boundary_complete = True
    if len(ordered) > 200:
        scores = truth.scores["dense"]
        cutoff = scores[ordered[199]]
        if np_count_ties(scores[ordered], cutoff) > 1:
            boundary_complete = set(truth.ids("dense", limit=200)).issubset(dense)
    if mode == "hybrid" and not boundary_complete:
        measurement["safe"] = False
    return {"ids": ids, "measurement": measurement, "finite_prefetch_measurement": finite,
            "full_fusion_candidate_coverage": len(set(truth.ids("hybrid", limit=10)) & (set(dense) | set(sparse)))
            / max(1, min(10, len(truth.orders["hybrid"]))) if mode == "hybrid" else None,
            "branch_boundary_complete": boundary_complete, "sql_diagnostic_ms": duration,
            "natural_plan": plan, "forced_hnsw_plan": forced}


def np_count_ties(scores, cutoff):
    return int((scores == cutoff).sum())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--per-cell", type=int, default=1, help="SQL smoke selection; omitted queries stay NOT_RUN")
    args = parser.parse_args()
    c = corpus.load(args.corpus)
    require_synthetic(c.manifest)
    queries_path = c.path / "heldout.jsonl"
    bindings = json.loads((c.path / "query-manifest.json").read_text())
    if (bindings["corpus_manifest"] != corpus.digest(c.path / "manifest.json")
            or bindings["queries"]["heldout"] != corpus.digest(queries_path)):
        raise ValueError("query/corpus binding drift")
    queries = [json.loads(line) for line in queries_path.read_text().splitlines()]
    rows, selected = [], defaultdict(int)
    stack = DisposablePostgres()
    try:
        with stack:
            install(stack.connection, c)
            for q in queries:
                cell = q["stratum"] + "/" + q["mode"]
                row = {"cell": cell, "query_id": q["id"], "status": "NOT_RUN"}
                if q["status"] == "READY" and selected[cell] < args.per_cell:
                    selected[cell] += 1
                    try:
                        result = evaluate(stack.connection, c, q)
                        row.update(status="MEASURED", **result["measurement"], diagnostic=result)
                    except (psycopg.Error, ValueError) as error:
                        row.update(status="ERROR", reason=type(error).__name__)
                rows.append(row)
    finally:
        report = {"schema": "cortex-b01-sql-diagnostic-v1", "binding": stack.binding,
                  "corpus_manifest_sha256": corpus.digest(c.path / "manifest.json"),
                  "query_manifest_sha256": corpus.digest(c.path / "query-manifest.json"),
                  "cleanup_verified": stack.cleanup_verified, "cells": oracle.cells(rows),
                  "engine_decision": "UNDECIDED", "product_path": False,
                  "not_run": ["real-data", "API/auth/provider/cache", "eight-client-load", "native-editions", "OS-cold",
                              "crash-ACK", "16-workers", "PITR", "snapshot-portability", "SELinux-24h"]}
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"engine_decision": "UNDECIDED", "cleanup_verified": stack.cleanup_verified,
                      "report_sha256": corpus.digest(args.output)}))


if __name__ == "__main__":
    main()
