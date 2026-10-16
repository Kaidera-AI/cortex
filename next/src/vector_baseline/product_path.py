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


class PreparedInput:
    """One privately retained snapshot/truth shared by the eight load clients.

    Construct before load.run. compare() is an OFFLINE seam: a refusal must be
    charged to its original offer, never omitted or converted to a safe result.
    """
    def __init__(self, root, spec, expected, source_root):
        c, spec, _, identity, _ = admit(root,spec,expected,spec['splits']['held_out'][0],source_root)
        self._corpus, self._spec, self.identity, self.input_sha256 = c, spec, identity, expected
        self._queries = {q['id']:q for q in spec['queries'] if q['id'] in spec['splits']['held_out']}
        if any(identity.dimensions != 768 or c.identity['metric'] != 'cosine'
                or identity.provider != 'openrouter' or q['mode'] != 'dense'
                or q['sparse'] is not None or q['cell'] != 'scope/dense'
                or any(q[k] is not None for k in ('lo','hi','kind','time_lo','time_hi'))
                for q in self._queries.values()):
            raise Refusal('product_contract_unbound')
        self._scopes = {(m['tenant'],m['project']):(UUID(m['tenant_id']),UUID(m['project_id']))
                        for m in spec['scope_map']}
        self._records = {r['id'].decode():r for r in c.records}
        self._reverse = {v:k for k,v in spec['record_map'].items()}
        self._truth = {qid:oracle.rank(c,q,modes=('dense',)) for qid,q in self._queries.items()}
        # Freeze the exact strict ranking AND every float64 score, including ties.
        truth_binding = {qid:[(rid,float(truth.score('dense',rid)))
                             for rid in truth.ids('dense',limit=len(c.records))]
                         for qid,truth in self._truth.items()}
        self.truth_sha256 = hashlib.sha256(canonical(truth_binding)).hexdigest()

    def compare(self, qid, response):
        """Validate retained serialized C11 bytes, then reuse the unchanged oracle."""
        try:
            q = self._queries[qid]
            if (response['query_id'] != qid or response['input_sha256'] != self.input_sha256
                    or response['truth_sha256'] != self.truth_sha256
                    or hashlib.sha256(response['body'].encode()).hexdigest() != response['response_sha256']):
                raise Refusal('response_binding_changed')
            body = json.loads(response['body']); fresh = body['freshness']
            if (body['query'] != q['text'] or body['complete'] is not True
                    or body['state'] not in ('ready','ready_empty') or fresh['state'] != 'current'
                    or fresh['complete'] is not True or type(fresh['pending_records']) is not int
                    or fresh['pending_records'] != 0 or fresh['identity'] != self.identity.key
                    or fresh['model_identity'] != self._spec['identity']
                    or type(body['results']) is not list or len(body['results']) > q['limit']
                    or (body['state'] == 'ready_empty') != (len(body['results']) == 0)):
                raise Refusal('response_unfulfilled')
            hits = []
            for hit in body['results']:
                sid = self._reverse.get(hit['id'])
                if sid is None: raise Refusal('hit_unbound')
                record = self._records[sid]
                if (record['tenant'].decode() != q['tenant'] or record['project'].decode() != q['project']
                        or bool(record['deleted']) or hit['kind'] != self._spec['kind_map'][str(int(record['kind']))]
                        or type(hit['revision']) is not int or hit['revision'] != self._spec['source_revisions'][sid]
                        or type(hit['distance']) not in (int,float) or not math.isfinite(hit['distance'])):
                    raise Refusal('hit_source_mismatch')
                hits.append(sid)
            measured = oracle.measure(self._truth[qid],hits,'dense',top_k=q['limit'])
            if not measured['safe']: raise Refusal('unsafe_hit_inventory')
            return measured
        except Refusal:
            raise
        except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
            raise Refusal('response_invalid') from None


def prepare_input(root, spec, expected, *, source_root):
    try:
        return PreparedInput(root,spec,expected,source_root)
    except Refusal:
        raise
    except (ValueError,TypeError,KeyError,IndexError,OSError,UnicodeError):
        raise Refusal('input_invalid') from None


def _observed_c11(source, owner):
    """Observe the existing C11/C04 cache and provider, without another cache.

    One instance per persistent client. Embed invocations are NOT HTTP-call or
    paid-call counts; the provider transport owner must supply those separately.
    All resources and their lifetime remain the explicit fixture owner's.
    """
    from cortex_core.api.c11b2 import C11b2ReadPort
    from types import SimpleNamespace
    if not isinstance(source,C11b2ReadPort) or source.identity != owner.frozen.identity:
        raise Refusal('c11_binding_invalid')

    async def embed(text, identity):
        owner._observed['provider_embed_calls'] += 1
        return await source.provider.embed(text,identity)

    class ObservedC11(C11b2ReadPort):
        def _cache(self, search):
            cache = super()._cache(search)
            async def get(*args):
                cached = await cache.get(*args)
                vector_literal(cached.vector,768)
                q = owner._query
                if np.asarray(cached.vector,dtype='<f4').tobytes() != np.asarray(q['vector'],dtype='<f4').tobytes():
                    raise Refusal('provider_vector_unbound')
                owner._observed['cache_hit'] = cached.cache_hit
                return cached
            return SimpleNamespace(get=get)

    return ObservedC11(source.record_port,source.pool,source.identity,SimpleNamespace(embed=embed),
        source.extractor_identity,source.project_repos,selection=source.selection,keyword=source.keyword,
        vector_projection=source.vector_projection)


class ProductClient:
    """SOURCE_FIXTURE load port. read_port selects the actual C11 component seam.

    Legacy direct-component calls remain diagnostic. Only the C11 mode moves
    independent truth out of timing. No public route or native capability is
    admitted by using a C11 handler and its existing serializer in a fixture.
    """
    def __init__(self, *args, **kwargs):
        self.arguments, self.options = args, kwargs
        self._prepared = None
        self.frozen = None
        self._read = None
        self._busy = False

    async def open(self):
        root, spec, expected, *_ = self.arguments
        try:
            if self.options.get('read_port') is not None:
                if not callable(self.options.get('scope_for')): raise Refusal('request_scope_unbound')
                frozen = self.options.get('prepared')
                if frozen is None: frozen = prepare_input(root,spec,expected,source_root=self.options['source_root'])
                if type(frozen) is not PreparedInput or frozen.input_sha256 != expected:
                    raise Refusal('input_binding_changed')
                self.frozen = frozen
                self._read = _observed_c11(self.options['read_port'],self)
                self._prepared = frozen
            else:
                self._prepared = admit(root, spec, expected, spec['splits']['held_out'][0],
                                       self.options['source_root'])
        except (Refusal, ValueError, TypeError, KeyError, OSError, UnicodeError):
            raise UnfulfilledResult('input_admission_failed') from None

    async def request(self, query):
        if self._prepared is None: raise UnfulfilledResult('client_not_open')
        if self._read is not None:
            return await self._c11_request(query)
        _, _, _, cache, search, authorize = self.arguments
        c, spec, _, identity, _ = self._prepared
        if query['id'] not in spec['splits']['held_out']:
            raise UnfulfilledResult('unapproved_query')
        q = next(q for q in spec['queries'] if q['id'] == query['id'])
        mapped = next(m for m in spec['scope_map'] if (m['tenant'], m['project']) == (q['tenant'], q['project']))
        scope = (UUID(mapped['tenant_id']), UUID(mapped['project_id']))
        row = await evaluate_admitted(c, spec, q, identity, scope, cache, search, authorize)
        if row["status"] != "MEASURED":
            raise UnfulfilledResult('unfulfilled_source_query')
        return row

    async def _c11_request(self, query):
        if self._prepared is None or self._busy: raise UnfulfilledResult('client_not_idle')
        self._busy = True
        try:
            import asyncio
            from cortex_core.api import c11a
            q = self.frozen._queries[query['id']]
            if canonical(query) != canonical(q): raise Refusal('offer_binding_changed')
            self._query = q
            self._observed = {'cache_hit':None,'provider_embed_calls':0}
            scope = copy.deepcopy(self.options['scope_for'](copy.deepcopy(q)))
            scope.update(method='POST',_c11b_body={'query':q['text'],'top_k':q['limit']})
            scope['_cortex_deadline'] = min(scope.get('_cortex_deadline',float('inf')),
                                           asyncio.get_running_loop().time()+10)
            principal = await self._read.principal(scope)
            if ((UUID(principal['tenant_id']),UUID(principal['project_id'])) !=
                    self.frozen._scopes[q['tenant'],q['project']]
                    or type(principal['permission_generation']) is not int
                    or principal['permission_generation'] < 1):
                raise Refusal('current_scope_mismatch')
            body = await self._read.search(principal,scope,None)
            messages = []
            from cortex_core.embeddings.keyword import check_deadline
            async def send(message):
                check_deadline(scope['_cortex_deadline'])
                messages.append(message)
            await c11a._respond(send,200,body)
            if (len(messages) != 2 or messages[0]['status'] != 200
                    or messages[1]['type'] != 'http.response.body' or messages[1].get('more_body',False)):
                raise Refusal('serialization_incomplete')
            data = messages[1]['body']
            return {'query_id':q['id'],'input_sha256':self.frozen.input_sha256,
                    'truth_sha256':self.frozen.truth_sha256,'body':data.decode(),
                    'response_sha256':hashlib.sha256(data).hexdigest(),**self._observed,
                    'source_fixture_only':True,'api_qualified':False}
        except Exception:
            raise UnfulfilledResult('unfulfilled_c11_source_query') from None
        finally:
            self._busy = False
            self._query = None

    def compare(self, query_id, response):
        return self.frozen.compare(query_id,response)

    async def prepare_measured(self, warmup_records, config):
        """After-warmup seam for Mike: refuse all native/cache cells for now.

        No owned native state controller or transport-call budget is bound here.
        Hashes bind this refusal to the admitted input, truth, config and exact
        retained warmup records. They cannot serve as cache/native proof or as a
        repetition receipt. Do not perform warmup/emptying/cold work here.
        """
        from dataclasses import asdict
        if self._prepared is None or self.frozen is None or self._busy:
            raise Refusal('client_not_idle')
        config.validate()
        binding = {'input_sha256':self.frozen.input_sha256,'truth_sha256':self.frozen.truth_sha256,
                   'config':asdict(config),'warmup_records':warmup_records}
        return {'status':'NOT_RUN','reason':'owned_measured_state_and_provider_budget_unbound',
                'capability':'UNAVAILABLE','sustained_all_miss':'NOT_RUN',
                'input_sha256':self.frozen.input_sha256,'truth_sha256':self.frozen.truth_sha256,
                'preparation_sha256':hashlib.sha256(canonical(binding)).hexdigest(),
                'warmup_errors':sum(r.get('status') != 'OK' for r in warmup_records)}

    async def aclose(self):
        self._prepared = None
        self._read = None
        # Retain frozen truth for Mike's offline join after load closes clients.

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
