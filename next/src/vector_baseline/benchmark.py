"""Synthetic-only offered-load PG diagnostic, using the reviewed owned lifecycle."""
import argparse
import asyncio
import copy
from dataclasses import asdict
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


def attach_product_truth(run, prepared):
    """Join retained C11 bytes to frozen truth OFFLINE, after client cleanup.

    Comparison refusal is a failure of the original offered request. Keep its
    bytes and clocks, and charge the same deadline penalty as a timed error.
    Existing failed offers and invalid inventories must never be repaired.
    """
    from .product_path import Refusal
    for phase in ("warmup_records", "records"):
        for row in run[phase]:
            if row["status"] != "OK":
                continue
            row.pop("measurement", None)
            row.pop("measurement_binding", None)
            try:
                measurement = prepared.compare(row["query_id"], row["response"])
                if not isinstance(measurement, dict) or measurement.get("safe") is not True:
                    raise Refusal("unsafe_offline_measurement")
                binding = {key: row["response"][key] for key in
                           ("query_id", "input_sha256", "truth_sha256", "response_sha256")}
                row["measurement"] = measurement
                row["measurement_binding"] = binding
            except Exception:
                row.update(status="ERROR", error_class="OfflineComparisonRefusal",
                           response_elapsed_seconds=row["latency_seconds"],
                           latency_seconds=max(run["config"]["deadline_seconds"], row["latency_seconds"]))


async def run_product(root, spec, expected, *, source_root, read_port, scope_for,
                      config=None, latency_budget_ms=100, clock=None, observer=None):
    """Bounded SOURCE_FIXTURE orchestration over S4a's frozen C11 seam.

    The caller owns every supplied port/resource. No allocator, credential
    discovery or native state admission lives here. Current S4a preparation
    refuses all native cells; source timing never upgrades that refusal.
    """
    from . import product_path
    config = load.RunConfig() if config is None else config
    config.validate()
    if (type(latency_budget_ms) not in (int, float) or not math.isfinite(latency_budget_ms)
            or latency_budget_ms <= 0):
        raise ValueError("positive finite latency budget required before input access")
    if read_port is None or not callable(scope_for):
        raise product_path.Refusal("c11_request_scope_unbound")
    frozen_spec = copy.deepcopy(spec)
    prepared = product_path.prepare_input(root, frozen_spec, expected, source_root=source_root)
    by_id = {q["id"]: q for q in frozen_spec["queries"]}
    queries = [by_id[qid] for qid in frozen_spec["splits"]["held_out"]]

    def factory(number):
        return product_path.client(root, frozen_spec, expected, None, None, None,
            source_root=source_root, read_port=read_port, scope_for=scope_for, prepared=prepared)

    async def prepare(clients, warmup, run_config):
        binding = {"input_sha256": prepared.input_sha256, "truth_sha256": prepared.truth_sha256,
                   "config": asdict(run_config), "warmup_records": warmup}
        refusal = {"status": "NOT_RUN", "reason": "owned_measured_state_and_provider_budget_unbound",
                   "capability": "UNAVAILABLE", "sustained_all_miss": "NOT_RUN",
                   "input_sha256": prepared.input_sha256, "truth_sha256": prepared.truth_sha256,
                   "preparation_sha256": hashlib.sha256(product_path.canonical(binding)).hexdigest(),
                   "warmup_errors": sum(row.get("status") != "OK" for row in warmup)}
        evidence = []
        for client in clients:
            value = await client.prepare_measured(copy.deepcopy(warmup), run_config)
            if (not isinstance(value, dict) or value != refusal
                    or type(value.get("warmup_errors")) is not int):
                raise product_path.Refusal("measured_preparation_unbound")
            evidence.append(copy.deepcopy(value))
        return evidence

    run = await load.run(queries, factory, config, clock=clock, observer=observer, before_measured=prepare)
    # Preparation binds timed warmup bytes, before offline annotations/penalties.
    run["preparation_warmup_records"] = copy.deepcopy(run["warmup_records"])
    attach_product_truth(run, prepared)
    result = report.summarize(run, latency_budget_ms=latency_budget_ms,
        bindings={"dataset": "synthetic", "boundary": "scheduled-arrival-to-serialized-C11-component-response",
                  "input_sha256": prepared.input_sha256, "truth_sha256": prepared.truth_sha256,
                  "heldout_query_ids": [q["id"] for q in queries], "scope": "SOURCE_FIXTURE"})
    result.update(qualification="C11_SOURCE_FIXTURE_DIAGNOSTIC", source_fixture_only=True,
                  native_qualified=False, api_qualified=False, whole_product_stack_complete=False,
                  sustained_all_miss="NOT_RUN", provider_embed_calls_are_http_calls=False)
    result["cells"] = [{"os_cache": os_cache, "query_cache": query_cache, "status": "NOT_RUN",
                        "reason": "owned_measured_state_and_provider_budget_unbound",
                        "input_sha256": prepared.input_sha256, "truth_sha256": prepared.truth_sha256}
                       for os_cache in ("warm", "cold") for query_cache in ("prefilled", "empty")]
    result["not_run"].extend(["OS-" + c["os_cache"] + "/query-" + c["query_cache"] for c in result["cells"]])
    result["not_run"].append("sustained-all-miss")
    return result


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
            engine="postgres", admission=None, frozen_input_sha256=None):
    if engine not in ("postgres", "qdrant"):
        raise ValueError("explicit supported benchmark engine required")
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError("positive finite latency budget required before input access")
    output = Path(output)
    if output.exists():
        raise ValueError("existing evidence cannot be overwritten")
    c = corpus.load(corpus_path) if admission is None else corpus.load(corpus_path, geometry_admission=admission)
    if "geometry" in c.manifest:
        if admission is not None:
            approved = admission.get("frozen_input_sha256")
            if frozen_input_sha256 is not None and frozen_input_sha256 != approved:
                raise ValueError("caller freeze differs from native import review")
            frozen_input_sha256 = approved
        c = geometry.load(corpus_path, admission=admission, frozen_input_sha256=frozen_input_sha256)
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
        result = not_run(c, cell, engine, selected, "unpopulated-filter-cell", frozen_input_sha256)
        with output.open("x") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        return result
    truth = {q["id"]: oracle.rank(c, q) for q in selected}
    config = load.RunConfig(duration_seconds=duration, warmup_seconds=warmup)
    config.validate()
    stack = postgres.DisposablePostgres() if engine == "postgres" else qdrant.DisposableQdrant()
    if engine == "postgres":
        stack.worker = "nemo"
    result = None
    failed = None
    stage = "setup"
    try:
        with stack:
            stage = "import"
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
            stage = "run"
            run = asyncio.run(load.run(selected, factory, config, observer=observer))
            attach_truth(run, truth)
            result = report.summarize(run, latency_budget_ms=budget_ms,
                                      bindings={"dataset": c.manifest["dataset"], "boundary": boundary, "engine": engine,
                                                "cell": cell, "identity": c.identity, "corpus_count": c.manifest["count"],
                                                "corpus_manifest_sha256": corpus.digest(c.path / "manifest.json"),
                                                "query_manifest_sha256": corpus.digest(c.path / "query-manifest.json"),
                                                "frozen_input_sha256": frozen_input_sha256,
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
            stage = "cleanup"
    except qdrant.ImportParityError:
        result = not_run(c, cell, engine, selected, "qdrant-import-parity-mismatch", frozen_input_sha256)
        result["engine_started"] = True
    except BaseException as error:
        failed = type(error).__name__
        if not isinstance(error, Exception):
            raise
    finally:
        if result is None:
            result = {"schema": "cortex-b02-run-v1", "engine_decision": "UNDECIDED", "gtm_qualified": False,
                      "diagnostic": {"verdict": "FAIL"}, "runner_error_class": failed,
                      "qualification": "INPUT_BOUND_OPERATION_FAILURE",
                      "bindings": {"dataset": c.manifest["dataset"], "engine": engine, "cell": cell, "identity": c.identity,
                                   "corpus_count": len(c.records), "frozen_input_sha256": frozen_input_sha256,
                                   "corpus_manifest_sha256": corpus.digest(c.path / "manifest.json"),
                                   "query_manifest_sha256": corpus.digest(c.path / "query-manifest.json")}}
        result["cleanup_verified"] = stack.cleanup_verified
        result["credential_discarded"] = stack.password is None
        result["lock_released"] = stack.lock is None
        if not stack.cleanup_verified or failed is not None:
            if result["diagnostic"]["verdict"] != "INVALID_HARNESS":
                result["diagnostic"]["verdict"] = "FAIL"
            result["runner_error_class"] = failed
            result["runner_failure"] = getattr(stack, "failure_receipt", None) or {
                "primary": {"phase": stage, "error_class": failed, "reason": "operation_failed"},
                "cleanup": None if stack.cleanup_verified else {"phase": "cleanup", "reason": "cleanup_incomplete"}}
        with output.open("x") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return result


def not_run(c, cell, engine, queries, reason, frozen_input_sha256):
    return {"schema": "cortex-b02-run-v1", "engine_decision": "UNDECIDED", "gtm_qualified": False,
            "qualification": "INPUT_BOUND_CELL_NOT_RUN", "engine_started": False,
            "bindings": {"dataset": c.manifest["dataset"], "identity": c.identity, "cell": cell,
                         "engine": engine, "corpus_count": len(c.records),
                         "corpus_manifest_sha256": corpus.digest(c.path / "manifest.json"),
                         "query_manifest_sha256": corpus.digest(c.path / "query-manifest.json"),
                         "frozen_input_sha256": frozen_input_sha256},
            "cells": [{"cell": cell, "status": "NOT_RUN", "reason": reason,
                       "missing_query_ids": [q["id"] for q in queries if q.get("status") != "READY"],
                       "query_ids": [q["id"] for q in queries]}],
            "diagnostic": {"verdict": "NOT_RUN", "recall_status": "NOT_RUN", "latency_status": "NOT_RUN",
                           "throughput_status": "NOT_RUN", "resource_collection_complete": False},
            "not_run": [cell], "cleanup_verified": True, "credential_discarded": True, "lock_released": True}


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
    parser.add_argument("--frozen-input-sha256")
    args = parser.parse_args()
    admission = json.loads(args.admission.read_text()) if args.admission else None
    result = execute(args.corpus, args.output, cell=args.cell, duration=args.duration,
                     warmup=args.warmup, budget_ms=args.latency_budget_ms, engine=args.engine, admission=admission,
                     frozen_input_sha256=args.frozen_input_sha256)
    print(json.dumps({"engine_decision": "UNDECIDED", "diagnostic": result["diagnostic"]["verdict"],
                      "cleanup_verified": result["cleanup_verified"],
                      "report_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))
    verdict = result["diagnostic"]["verdict"]
    return 2 if verdict == "INVALID_HARNESS" else 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
