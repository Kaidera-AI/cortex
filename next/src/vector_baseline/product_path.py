"""Synthetic product-component query proof; never native/API/GTM acceptance.

Ports come from the fixture owner. This module neither creates resources nor
implements Core authorization, provider discovery or missing product filters.
"""
import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import tempfile
from uuid import UUID

import numpy as np
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable, CoreUnavailable, EmbeddingIdentity, vector_literal,
)
from . import corpus, oracle


class Refusal(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class UnfulfilledResult(RuntimeError):
    """A fixed class, so load accounting charges failures without leaking text."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def snapshot_corpus(root, manifest_bytes):
    """Stage once-read, hash-checked fixture bytes; retain private immutable arrays."""
    manifest = json.loads(manifest_bytes)
    if manifest.get("dataset") != "synthetic" or set(manifest["files"]) != set(corpus.FILES):
        raise Refusal("synthetic_input_required")
    total = 0
    with tempfile.TemporaryDirectory(prefix="b01-validated-corpus-") as directory:
        stage = Path(directory)
        (stage / "manifest.json").write_bytes(manifest_bytes)
        for name in corpus.FILES:
            with (Path(root) / name).open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            total += len(data)
            if total > 32 * 1024 * 1024:
                raise Refusal("source_fixture_bound_exceeded")
            if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
                raise Refusal("corpus_binding_changed")
            (stage / name).write_bytes(data)
        staged = corpus.load(stage)
        arrays = {name: np.array(getattr(staged, name), copy=True)
                  for name in ("vectors", "records", "offsets", "indices", "weights")}
        for array in arrays.values(): array.setflags(write=False)
        return replace(staged, path=Path(root), manifest=copy.deepcopy(staged.manifest), **arrays)


def admit(root, spec, expected, qid, source_root):
    # Snapshot metadata once; no caller mutation can change a request after check.
    spec = copy.deepcopy(spec)
    if hashlib.sha256(canonical(spec)).hexdigest() != expected:
        raise Refusal("input_binding_changed")
    required = {"schema", "identity", "corpus_manifest_sha256", "queries", "splits",
                "scope_map", "kind_map", "record_map", "source_revisions", "source_sha256"}
    if set(spec) != required or spec["schema"] != "cortex.b01-product-input.v1":
        raise Refusal("input_shape_invalid")
    with (Path(root) / "manifest.json").open("rb") as stream:
        manifest_bytes = stream.read(1024 * 1024 + 1)
    if hashlib.sha256(manifest_bytes).hexdigest() != spec["corpus_manifest_sha256"]:
        raise Refusal("corpus_binding_changed")
    c = snapshot_corpus(root, manifest_bytes)
    if c.manifest["dataset"] != "synthetic":
        raise Refusal("synthetic_input_required")
    for relative, digest in spec["source_sha256"].items():
        path = Path(relative)
        if (path.is_absolute() or ".." in path.parts or not relative.startswith("src/cortex_core/")
                or not (Path(source_root) / path).resolve().is_relative_to(Path(source_root).resolve())
                or corpus.digest(Path(source_root) / path) != digest):
            raise Refusal("source_binding_changed")
    if not spec["source_sha256"]:
        raise Refusal("source_binding_missing")
    identity = spec["identity"]
    if (set(identity) != {"provider", "model", "version", "dimensions", "preprocessing"}
            or type(identity["dimensions"]) is not int
            or any(not isinstance(identity[key], str) or not identity[key].strip()
                   for key in ("provider", "model", "version", "preprocessing"))
            or (identity["provider"], identity["model"], identity["dimensions"]) !=
               (c.identity["provider"], c.identity["model"], c.identity["dimension"])):
        raise Refusal("identity_mismatch")
    queries = spec["queries"]
    ids = [q["id"] for q in queries]
    if not ids or len(ids) != len(set(ids)) or any(not isinstance(x, str) or not x for x in ids):
        raise Refusal("query_identity_invalid")
    splits = spec["splits"]
    if set(splits) != {"tuning", "held_out"} or set(splits["tuning"]) & set(splits["held_out"]):
        raise Refusal("split_overlap")
    if (len(splits["tuning"]) != len(set(splits["tuning"]))
            or len(splits["held_out"]) != len(set(splits["held_out"]))
            or set(ids) != set(splits["tuning"]) | set(splits["held_out"])):
        raise Refusal("split_coverage_invalid")
    if qid not in splits["held_out"]:
        raise Refusal("held_out_query_required")
    q = next(q for q in queries if q["id"] == qid)
    for candidate in queries:
        oracle.validate_query(c, candidate)
        if (not isinstance(candidate["text"], str) or not candidate["text"]
                or len(candidate["text"].encode()) > 16384
                or not isinstance(candidate["subject"], str) or not candidate["subject"]
                or type(candidate["limit"]) is not int or not 1 <= candidate["limit"] <= 100):
            raise Refusal("query_shape_invalid")
    maps = {(m["tenant"], m["project"]): (UUID(m["tenant_id"]), UUID(m["project_id"]))
            for m in spec["scope_map"]}
    if len(maps) != len(spec["scope_map"]) or len(set(maps.values())) != len(maps):
        raise Refusal("scope_mapping_not_bijective")
    scope = maps.get((q["tenant"], q["project"]))
    if scope is None or any((candidate["tenant"], candidate["project"]) not in maps for candidate in queries):
        raise Refusal("scope_mapping_missing")
    records = {r["id"].decode(): r for r in c.records}
    mapping, revisions = spec["record_map"], spec["source_revisions"]
    if (set(mapping) != set(records) or set(revisions) != set(records)
            or len(set(mapping.values())) != len(mapping)
            or any(not isinstance(v, str) or not v for v in mapping.values())
            or any(type(v) is not int or v < 1 for v in revisions.values())
            or any((r["tenant"].decode(), r["project"].decode()) not in maps for r in records.values())
            or any(str(int(r["kind"])) not in spec["kind_map"] for r in records.values())):
        raise Refusal("record_mapping_invalid")
    return c, spec, q, EmbeddingIdentity(**identity), scope


async def evaluate(root, spec, expected, qid, cache, search, authorize, *, source_root):
    """Return recall only after the declared input and actual response agree."""
    try:
        c, spec, q, identity, expected_scope = admit(root, spec, expected, qid, source_root)
    except Refusal as error:
        return {"status": "INPUT_REFUSED", "reason": error.reason}
    except (ValueError, TypeError, KeyError, OSError, UnicodeError):
        return {"status": "INPUT_REFUSED", "reason": "input_invalid"}
    return await evaluate_admitted(c, spec, q, identity, expected_scope, cache, search, authorize)


async def evaluate_admitted(c, spec, q, identity, expected_scope, cache, search, authorize):
    qid = q["id"]
    if (identity.dimensions != 768 or c.identity["metric"] != "cosine"
            or identity.provider != "openrouter" or q["mode"] != "dense"
            or q["sparse"] is not None or q["cell"] != "scope/dense"
            or any(q[k] is not None for k in ("lo", "hi", "kind", "time_lo", "time_hi"))):
        return {"status": "NOT_RUN", "reason": "product_contract_unbound", "query_id": qid}
    try:
        vector_literal(q["vector"], 768)
        current = await authorize(q["subject"])
        if ((current.tenant_id, current.project_id) != expected_scope
                or type(current.permission_generation) is not int or current.permission_generation < 1):
            return {"status": "INPUT_REFUSED", "reason": "current_scope_mismatch"}
        cached = await cache.get(q["subject"], identity, q["text"])
        vector_literal(cached.vector, 768)
        if np.asarray(cached.vector, dtype="<f4").tobytes() != np.asarray(q["vector"], dtype="<f4").tobytes():
            return {"status": "RESULT_REFUSED", "reason": "provider_vector_unbound"}
        result = await search.search(q["subject"], identity, cached.vector, limit=q["limit"])
    except (CapabilityUnavailable, CoreUnavailable, PermissionError, TimeoutError):
        return {"status": "UNAVAILABLE", "reason": "product_unavailable", "query_id": qid}
    except (ValueError, TypeError):
        return {"status": "RESULT_REFUSED", "reason": "product_vector_invalid"}
    if result.freshness.identity != identity.key:
        return {"status": "RESULT_REFUSED", "reason": "result_identity_mismatch"}
    reverse = {product_id: sid for sid, product_id in spec["record_map"].items()}
    rows = {r["id"].decode(): r for r in c.records}
    hits = []
    for hit in result.hits:
        sid = reverse.get(hit.record_id)
        if sid is None:
            return {"status": "RESULT_REFUSED", "reason": "hit_unbound"}
        record = rows[sid]
        if (record["tenant"].decode() != q["tenant"] or record["project"].decode() != q["project"]
                or bool(record["deleted"]) or hit.kind != spec["kind_map"][str(int(record["kind"]))]
                or type(hit.source_revision) is not int or hit.source_revision != spec["source_revisions"][sid]
                or not math.isfinite(hit.distance)):
            return {"status": "RESULT_REFUSED", "reason": "hit_source_mismatch"}
        hits.append(sid)
    truth = oracle.rank(c, q, modes=("dense",))
    measured = oracle.measure(truth, hits, "dense", top_k=q["limit"])
    if not measured["safe"]:
        return {"status": "RESULT_REFUSED", "reason": "unsafe_hit_inventory"}
    return {"status": "MEASURED", "reason": "measured_source_fixture", "query_id": qid, "ids": hits, "measurement": measured,
            "cache_hit": cached.cache_hit, "identity": identity.key,
            "source_fixture_only": True}


class ProductClient:
    """Cooperatively cancellable load port; shared resource lifetime is external."""
    def __init__(self, *args, **kwargs):
        self.arguments, self.options = args, kwargs
        self._prepared = None

    async def open(self):
        root, spec, expected, *_ = self.arguments
        try:
            self._prepared = admit(root, spec, expected, spec["splits"]["held_out"][0],
                                   self.options["source_root"])
        except (Refusal, ValueError, TypeError, KeyError, OSError, UnicodeError):
            raise UnfulfilledResult("input_admission_failed") from None

    async def request(self, query):
        _, _, _, cache, search, authorize = self.arguments
        c, spec, _, identity, _ = self._prepared
        if query["id"] not in spec["splits"]["held_out"]:
            raise UnfulfilledResult("unapproved_query")
        q = next(q for q in spec["queries"] if q["id"] == query["id"])
        mapped = next(m for m in spec["scope_map"] if (m["tenant"], m["project"]) == (q["tenant"], q["project"]))
        scope = (UUID(mapped["tenant_id"]), UUID(mapped["project_id"]))
        row = await evaluate_admitted(c, spec, q, identity, scope, cache, search, authorize)
        if row["status"] != "MEASURED":
            raise UnfulfilledResult("unfulfilled_source_query")
        return row

    async def aclose(self):
        self._prepared = None


def client(*args, **kwargs):
    return ProductClient(*args, **kwargs)


def receipt(run, input_sha256, *, verification_kind, cleanup):
    if verification_kind not in ("UNIT_PORTS", "REAL_PG_COMPONENTS"):
        raise Refusal("verification_kind_invalid")
    if (run["clients_opened"] != 8 or run["clients_closed"] != 8 or run["close_errors"]
            or cleanup.get("complete") is not True):
        raise Refusal("cleanup_incomplete")
    for name, duration in (("records", run["config"]["duration_seconds"]),
                           ("warmup_records", run["config"]["warmup_seconds"])):
        rows = run[name]
        if (len(rows) != round(duration * 40)
                or [r["offer_id"] for r in rows] != list(range(len(rows)))):
            raise Refusal("offer_inventory_incomplete")
    return {"schema": "cortex.b01-product-proof.v1", "scope": "SOURCE_FIXTURE",
            "verification_kind": verification_kind, "input_sha256": input_sha256,
            "load": copy.deepcopy(run), "cleanup": copy.deepcopy(cleanup),
            "native_qualified": False, "api_qualified": False, "real_data_qualified": False,
            "whole_product_stack_complete": False, "gtm_qualified": False,
            "engine_decision": "UNDECIDED", "missing_gates": ["native", "HTTP/API", "real-model/data",
                "full-filter/sparse/hybrid", "full-stack", "inherited-exact-head-PG-manifest"]}
