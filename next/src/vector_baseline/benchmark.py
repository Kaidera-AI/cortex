"""Synthetic-only offered-load PG diagnostic, using the reviewed owned lifecycle."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import psycopg

from . import corpus, geometry, load, oracle, postgres, qdrant, report

OBSERVATION_TIMEOUT_SECONDS = 5


def admit(manifest):
    if manifest.get("dataset") != "synthetic" or manifest.get("remote", False) is not False:
        raise ValueError("only locally owned synthetic fixtures admitted")


def attach_truth(run, truth):
    for row in run["records"]:
        if row["status"] == "OK":
            row["measurement"] = oracle.measure(truth[row["query_id"]], row["response"]["ids"], "dense")


class SQLClient:
    def __init__(self, stack, metric):
        self.stack, self.metric, self.connection, self.backend_id = stack, metric, None, None

    async def open(self):
        info = self.stack.connection.info
        self.connection = await psycopg.AsyncConnection.connect(host=info.host, port=info.port,
                                                                dbname=info.dbname, user=info.user,
                                                                password=self.stack.password,
                                                                autocommit=True, connect_timeout=2)
        cursor = await self.connection.execute("SELECT pg_backend_pid()")
        self.backend_id = (await cursor.fetchone())[0]

    async def request(self, query):
        async with self.connection.transaction():
            for key in ("ef_search", "iterative_scan", "max_scan_tuples", "scan_mem_multiplier"):
                await self.connection.execute("SELECT set_config(%s,%s,true)",
                                              ["hnsw." + key, str(postgres.SETTINGS[key])])
            await self.connection.execute("SET LOCAL statement_timeout='10s'")
            sql, params = postgres.dense_query(query, self.metric)
            cursor = await self.connection.execute(sql, params)
            rows = await cursor.fetchall()
        return {"ids": [row[0] for row in rows[:10]], "connection_id": self.backend_id}

    async def aclose(self):
        if self.connection is not None:
            await self.connection.close()
            self.connection = None


async def observe_owned(stack):
    # Verify lifecycle before a read; never collect unrelated engine resources or env.
    target = await asyncio.to_thread(stack.owned_resource, "container")
    if target is None:
        raise RuntimeError("owned observation identity lost")
    process = await asyncio.create_subprocess_exec("podman", "stats", "--no-stream", "--format", "json", target,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), OBSERVATION_TIMEOUT_SECONDS)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        raise RuntimeError("owned stats failed")
    stats = json.loads(stdout)
    return {"scope": "owned-synthetic-PG-only", "whole_product_stack_complete": False,
            "lifecycle": stack.lifecycle, "stats": stats,
            "missing": ["API", "provider/cache", "Conductor", "backup/rebuild", "native8GB-stack"]}


async def observe_qdrant(stack):
    q = await observe_owned(stack)
    adapter = SimpleNamespace(lifecycle=stack.lifecycle,
                              owned_resource=lambda resource: stack.owned_resource(resource, stack.client_name))
    client = await observe_owned(adapter)
    q["scope"], client["scope"] = "owned-synthetic-Qdrant-only", "owned-synthetic-client-only"
    return {"scope": "owned-Qdrant-and-proxy-diagnostic", "whole_product_stack_complete": False,
            "components": {"qdrant": q, "client": client}, "lifecycle": stack.lifecycle}


def execute(corpus_path, output, *, cell="scope/dense", duration=300, warmup=60, budget_ms=100,
            engine="postgres", admission=None):
    if engine not in ("postgres", "qdrant"):
        raise ValueError("explicit supported benchmark engine required")
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError("positive finite latency budget required before input access")
    output = Path(output)
    if output.exists():
        raise ValueError("existing evidence cannot be overwritten")
    c = corpus.load(corpus_path) if admission is None else corpus.load(corpus_path, geometry_admission=admission)
    if "geometry" in c.manifest:
        c = geometry.load(corpus_path, admission=admission)
    if admission is None:
        admit(c.manifest)
    else:
        geometry.admit_native(c.manifest, admission)
    if c.manifest.get("remote", False) is not False:
        raise ValueError("existing remote services are never admitted")
    if cell not in {s + "/dense" for s in corpus.STRATA}:
        raise ValueError("geometry diagnostic supports explicit dense cells only")
    query_path = c.path / "heldout.jsonl"
    bindings = json.loads((c.path / "query-manifest.json").read_text())
    if (bindings["corpus_manifest"] != corpus.digest(c.path / "manifest.json")
            or bindings["queries"]["heldout"] != corpus.digest(query_path)):
        raise ValueError("frozen input binding drift")
    queries = [json.loads(line) for line in query_path.read_text().splitlines()]
    selected = [q for q in queries if q["stratum"] + "/" + q["mode"] == cell]
    if not selected or any(q["status"] != "READY" for q in selected):
        raise ValueError("unpopulated filter cell, NOT_RUN")
    truth = {q["id"]: oracle.rank(c, q) for q in selected}
    config = load.RunConfig(duration_seconds=duration, warmup_seconds=warmup)
    config.validate()
    stack = postgres.DisposablePostgres() if engine == "postgres" else qdrant.DisposableQdrant()
    if engine == "postgres":
        stack.worker = "nemo"
    result = None
    failed = None
    try:
        with stack:
            if engine == "postgres":
                if admission is None:
                    postgres.install(stack.connection, c)
                else:
                    postgres.install(stack.connection, c, admission=admission)
                sql, params = postgres.dense_query(selected[0], c.identity["metric"])
                natural_plan = [row[0] for row in stack.connection.execute("EXPLAIN " + sql, params)]
                db_bytes = stack.connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
                factory = lambda _: SQLClient(stack, c.identity["metric"])
                observer = lambda: observe_owned(stack)
                boundary = "scheduled-arrival-to-SQL-response"
            else:
                qdrant.install(stack, c, admission=admission)
                natural_plan, db_bytes = [], None
                factory = lambda _: qdrant.Client(stack)
                observer = lambda: observe_qdrant(stack)
                boundary = "scheduled-arrival-to-proxy-HTTP-response-including-IPC"
            run = asyncio.run(load.run(selected, factory, config, observer=observer))
            attach_truth(run, truth)
            result = report.summarize(run, latency_budget_ms=budget_ms,
                                      bindings={"dataset": c.manifest["dataset"], "boundary": boundary, "engine": engine,
                                                "cell": cell, "identity": c.identity, "corpus_count": c.manifest["count"],
                                                "corpus_manifest_sha256": corpus.digest(c.path / "manifest.json"),
                                                "query_manifest_sha256": corpus.digest(c.path / "query-manifest.json"),
                                                "heldout_queries_in_cell": len(selected),
                                                "heldout_query_ids": [q["id"] for q in selected], "stack": stack.binding,
                                                "natural_plan": natural_plan, "database_bytes": db_bytes})
            if engine == "qdrant":
                result["qualification"] = "SYNTHETIC_QDRANT_PROXY_DIAGNOSTIC"
            if "geometry" in c.manifest:
                result["bindings"]["geometry"] = c.manifest["geometry"]
                result["not_run"].extend("geometry-category:" + json.dumps(row["category"])
                                         for row in c.manifest["geometry"]["coverage"] if row["status"] == "NOT_RUN")
            if admission is not None:
                result["qualification"] = "ADMITTED_GEOMETRY_ENGINE_DIAGNOSTIC"
                result["bindings"]["native_admission"] = admission
    except BaseException as error:
        failed = type(error).__name__
        if not isinstance(error, Exception):
            raise
    finally:
        if result is None:
            result = {"schema": "cortex-b02-run-v1", "engine_decision": "UNDECIDED", "gtm_qualified": False,
                      "diagnostic": {"verdict": "FAIL"}, "runner_error_class": failed}
        result["cleanup_verified"] = stack.cleanup_verified
        result["credential_discarded"] = stack.password is None
        result["lock_released"] = stack.lock is None
        if not stack.cleanup_verified or failed is not None:
            result["diagnostic"]["verdict"] = "FAIL"
            result["runner_error_class"] = failed
        with output.open("x") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cell", default="scope/dense")
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--warmup", type=float, default=60)
    parser.add_argument("--latency-budget-ms", type=float, default=100)
    parser.add_argument("--engine", choices=("postgres", "qdrant"), default="postgres")
    parser.add_argument("--admission", type=Path)
    args = parser.parse_args()
    admission = json.loads(args.admission.read_text()) if args.admission else None
    result = execute(args.corpus, args.output, cell=args.cell, duration=args.duration,
                     warmup=args.warmup, budget_ms=args.latency_budget_ms, engine=args.engine, admission=admission)
    print(json.dumps({"engine_decision": "UNDECIDED", "diagnostic": result["diagnostic"]["verdict"],
                      "cleanup_verified": result["cleanup_verified"],
                      "report_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))
    return 1 if result["diagnostic"]["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
