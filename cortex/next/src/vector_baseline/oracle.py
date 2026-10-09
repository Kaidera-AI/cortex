"""Exhaustive canonical truth, never defined by Postgres candidates."""
from dataclasses import dataclass
import numpy as np
from .corpus import sparse_check, vector_check


def validate_query(c, q):
    if q["status"] != "READY" or q["generation"] != c.identity["generation"]:
        raise ValueError("unready or stale-generation query")
    if any(not isinstance(q[k], str) or not q[k] for k in ("tenant", "project")):
        raise ValueError("authoritative tenant/project required")
    vector_check(q["vector"], c.identity)
    sparse_check(q["sparse"])
    if np.asarray(q["vector"]).ndim != 1:
        raise ValueError("query must contain one vector")
    for k in ("lo", "hi", "kind", "time_lo", "time_hi"):
        if q[k] is not None and type(q[k]) is not int:
            raise ValueError("typed filter required")


def eligible(records, q):
    keep = ((records["tenant"] == q["tenant"].encode()) & (records["project"] == q["project"].encode())
            & ~records["deleted"])
    for key, field, comparison in (("lo", "ordinal", np.greater_equal), ("hi", "ordinal", np.less_equal),
                                    ("kind", "kind", np.equal), ("time_lo", "time", np.greater_equal),
                                    ("time_hi", "time", np.less_equal)):
        if q[key] is not None:
            keep &= comparison(records[field], q[key])
    return keep


def ordered(c, indices, scores):
    return indices[np.lexsort((c.records["id"][indices], -scores[indices]))]


def fusion(c, dense, sparse, prefetch=None):
    score = np.zeros(len(c.records), dtype=np.float64)
    for branch in (dense, sparse):
        selected = branch if prefetch is None else branch[:prefetch]
        score[selected] += 1 / (2 + np.arange(len(selected), dtype=np.float64))
    candidates = np.flatnonzero(score > 0)
    return ordered(c, candidates, score), score


@dataclass
class Truth:
    corpus: object
    eligible_indices: np.ndarray
    orders: dict
    scores: dict
    available: dict

    @property
    def eligible_count(self):
        return len(self.eligible_indices)

    def key(self, mode, full=True):
        if not self.available.get(mode, False):
            raise ValueError("missing actual sparse features: NOT_RUN")
        return "hybrid_finite" if mode == "hybrid" and not full else mode

    def ids(self, mode, *, full=True, limit=100):
        return [x.decode("ascii") for x in self.corpus.records["id"][self.orders[self.key(mode, full)][:limit]]]

    def score(self, mode, sid):
        idx = np.flatnonzero(self.corpus.records["id"] == sid.encode())
        return float(self.scores[self.key(mode)][idx[0]])


def rank(c, q, *, block_size=4096, prefetch=200, modes=("dense", "sparse", "hybrid")):
    validate_query(c, q)
    if block_size < 1 or prefetch < 1:
        raise ValueError("invalid oracle block/prefetch")
    n = len(c.records)
    keep = np.zeros(n, dtype=bool)
    dense_score = np.full(n, -np.inf, dtype=np.float64)
    sparse_score = np.zeros(n, dtype=np.float64)
    qv = np.asarray(q["vector"], dtype=np.float64)
    qs = dict(q["sparse"] or [])
    for start in range(0, n, block_size):
        end = min(start + block_size, n)
        selected = np.flatnonzero(eligible(c.records[start:end], q)) + start
        keep[selected] = True
        if not modes:
            continue
        v = c.vectors[selected].astype(np.float64)
        metric = c.identity["metric"]
        if metric == "dot":
            dense_score[selected] = np.sum(v * qv, axis=1)
        elif metric == "cosine":
            dense_score[selected] = np.sum(v * qv, axis=1) / (np.sqrt(np.sum(v * v, axis=1)) * np.sqrt(np.sum(qv * qv)))
        else:
            dense_score[selected] = -np.sqrt(np.sum((v - qv) ** 2, axis=1))
        # Independent sparse dot over canonical CSR, not SQL/engine ranks.
        lo, hi = int(c.offsets[start]), int(c.offsets[end])
        if hi > lo and qs:
            counts = np.diff(c.offsets[start:end + 1])
            owners = np.repeat(np.arange(end - start), counts)
            weights = c.weights[lo:hi].astype(np.float64)
            terms = c.indices[lo:hi]
            products = np.zeros(hi - lo, dtype=np.float64)
            for term, weight in qs.items():
                products[terms == term] = weights[terms == term] * weight
            sums = np.bincount(owners, weights=products, minlength=end - start)
            sparse_score[selected] = sums[selected - start]
    idx = np.flatnonzero(keep)
    if not modes:
        return Truth(c, idx, {}, {}, {})
    dense = ordered(c, idx, dense_score)
    sparse = ordered(c, idx[sparse_score[idx] > 0], sparse_score)
    full, full_score = fusion(c, dense, sparse)
    finite, finite_score = fusion(c, dense, sparse, prefetch)
    actual_sparse = q["sparse"] is not None and bool(np.all(c.records["has_sparse"][idx]))
    return Truth(c, idx, {"dense": dense, "sparse": sparse, "hybrid": full, "hybrid_finite": finite},
                 {"dense": dense_score, "sparse": sparse_score, "hybrid": full_score, "hybrid_finite": finite_score},
                 {"dense": True, "sparse": actual_sparse, "hybrid": actual_sparse})


def measure(truth, hits, mode, *, top_k=10, full=True):
    key = truth.key(mode, full)
    ranking, scores = truth.orders[key], truth.scores[key]
    k = min(top_k, len(ranking))
    ids = truth.corpus.records["id"]
    wanted = set(truth.ids(mode, full=full, limit=k))
    hit_set = set(hits)
    valid = np.flatnonzero(np.isin(ids, [x.encode() for x in hit_set]))
    allowed = np.isin(valid, ranking)
    safe = (len(hits) == len(hit_set) == k and len(valid) == len(hit_set) and bool(np.all(allowed)))
    if not k:
        return {"safe": safe, "strict_recall": None, "tie_recall": None, "eligible_count": truth.eligible_count,
                "expected_count": 0, "empty_exact": not hits}
    cutoff = scores[ranking[k - 1]]
    strict_above = ranking[scores[ranking] > cutoff]
    tied = ranking[scores[ranking] == cutoff]
    above_hits = int(np.count_nonzero(np.isin(valid, strict_above)))
    tie_hits = int(np.count_nonzero(np.isin(valid, tied)))
    return {"safe": safe, "strict_recall": len(wanted & hit_set) / k,
            "tie_recall": (above_hits + min(k - len(strict_above), tie_hits)) / k,
            "eligible_count": truth.eligible_count, "expected_count": k, "boundary_ties": len(tied)}


def cells(rows):
    result = {}
    for name in sorted({r["cell"] for r in rows}):
        group = [r for r in rows if r["cell"] == name]
        measured = [r for r in group if r["status"] == "MEASURED"]
        values = [r["tie_recall"] for r in measured if r["tie_recall"] is not None]
        safety = all(r["safe"] for r in measured)
        mean = float(np.mean(values)) if values else None
        verdict = ("NOT_RUN" if len(measured) != len(group) or not values else
                   "PASS" if safety and mean >= .95 else "FAIL")
        result[name] = {"verdict": verdict, "mean_recall": mean, "minimum": min(values) if values else None,
                        "failed_queries": [i for i, r in enumerate(group) if r.get("status") == "MEASURED"
                                           and (not r["safe"] or (r["tie_recall"] is not None and r["tie_recall"] < .95))],
                        "queries": group}
    return result


def engine_decision(cell_results, *, dataset, product_path, complete=False):
    """Only complete, real-data product-path search evidence can trigger an engine ruling."""
    if (dataset != "marlow-4oct" or not product_path or not complete or not cell_results
            or any(c["verdict"] == "NOT_RUN" for c in cell_results.values())):
        return "UNDECIDED"
    return "REOPEN_QDRANT" if any(c["verdict"] == "FAIL" for c in cell_results.values()) else "POSTGRES_FIRST"
