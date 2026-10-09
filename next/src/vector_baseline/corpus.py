"""Disk-backed frozen inputs; independent of search engines and providers."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from dataclasses import dataclass
import numpy as np

STRATA = ("scope", "broad", "medium", "tight", "very_tight", "edges")
RECORD = np.dtype([("id", "S64"), ("tenant", "S64"), ("project", "S64"),
                   ("kind", "<i4"), ("time", "<i8"), ("ordinal", "<i8"),
                   ("deleted", "?"), ("has_sparse", "?")])
FILES = ("vectors.npy", "records.npy", "sparse_offsets.npy", "sparse_indices.npy", "sparse_values.npy")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def identity_check(identity):
    if set(identity) != {"provider", "model", "dimension", "metric", "generation"}:
        raise ValueError("explicit vector identity required")
    if (type(identity["dimension"]) is not int or not 1 <= identity["dimension"] <= 2000
            or identity["metric"] not in ("cosine", "dot", "euclidean")
            or any(not isinstance(identity[k], str) or not identity[k] for k in ("provider", "model", "generation"))):
        raise ValueError("invalid identity or unsupported HNSW dimension")


def sparse_check(sparse):
    previous = -1
    for term, weight in sparse or []:
        if type(term) is not int or not previous < term <= 2**31 - 1 or not np.isfinite(weight) or weight <= 0:
            raise ValueError("sparse terms must be sorted/unique with positive finite weights")
        previous = term


def vector_check(vector, identity):
    a = np.asarray(vector, dtype=np.float64)
    if a.shape[-1:] != (identity["dimension"],) or not np.isfinite(a).all():
        raise ValueError("invalid dense vector")
    if identity["metric"] == "cosine" and np.any(np.linalg.norm(a, axis=-1) == 0):
        raise ValueError("zero cosine vector")
    stored = a.astype("<f4")
    if not np.isfinite(stored).all():
        raise ValueError("dense values exceed float32")
    if identity["metric"] == "cosine" and np.any(np.linalg.norm(stored.astype(np.float64), axis=-1) == 0):
        raise ValueError("stored float32 cosine vector is zero")


def validate_sanitized_row(row, identity):
    """Typed real-data slot; no text, identity recovery, provider or live access."""
    identity_check(identity)
    required = {"id", "tenant", "project", "kind", "time", "ordinal", "deleted", "model", "vector", "sparse"}
    if set(row) != required:
        raise ValueError("missing or forbidden sanitized fields")
    if any(not isinstance(row[k], str) or not re.fullmatch(r"[0-9a-f]{64}", row[k]) for k in ("id", "tenant", "project")):
        raise ValueError("real IDs require pseudonymous HMAC hex")
    if row["model"] != identity["model"] or type(row["deleted"]) is not bool:
        raise ValueError("mixed model or invalid tombstone")
    if any(type(row[k]) is not int for k in ("kind", "time", "ordinal")):
        raise ValueError("coarse fields require integers")
    vector_check(row["vector"], identity)
    sparse_check(row["sparse"])
    return dict(row)


@dataclass
class Corpus:
    path: Path
    manifest: dict
    vectors: np.ndarray
    records: np.ndarray
    offsets: np.ndarray
    indices: np.ndarray
    weights: np.ndarray

    @property
    def identity(self):
        return self.manifest["identity"]


def array(path, dtype, shape):
    return np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape, version=(2, 0))


def finish(path, identity, count, **extra):
    manifest = {"schema": "cortex-b01-corpus-v1", "dataset": "synthetic", "count": count,
                "identity": identity, "dtype": "little-endian-float32", "normalization": "stored-values",
                "sparse": "positive-weighted-dot-v1", "id_order": "ascii-ascending",
                "deletion_revision": "frozen-1", "files": {f: digest(path / f) for f in FILES}, **extra}
    (path / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return load(path)


def write_corpus(path, vectors, rows, identity):
    """Public fixtures. Real conversion remains under the export owner's custody."""
    identity_check(identity)
    a = np.asarray(vectors, dtype="<f4")
    if a.ndim != 2 or len(a) != len(rows) or len(a) == 0:
        raise ValueError("invalid corpus shape")
    vector_check(a, identity)
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    v = array(path / "vectors.npy", "<f4", a.shape)
    v[:] = a
    records = array(path / "records.npy", RECORD, (len(rows),))
    offsets, indices, weights = [0], [], []
    for i, row in enumerate(rows):
        if set(row) - {"id", "tenant", "project", "kind", "time", "ordinal", "deleted", "sparse"}:
            raise ValueError("unexpected fixture fields")
        for field in ("id", "tenant", "project"):
            value = row.get(field, "t1" if field == "tenant" else "p1")
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
                raise ValueError("invalid comparison ID")
            records[field][i] = value.encode("ascii")
        for field in ("kind", "time", "ordinal"):
            records[field][i] = row.get(field, i if field == "ordinal" else 0)
        records["deleted"][i] = row.get("deleted", False)
        sparse = row.get("sparse")
        sparse_check(sparse)
        records["has_sparse"][i] = sparse is not None
        for term, weight in sparse or []:
            indices.append(term)
            weights.append(weight)
        offsets.append(len(indices))
    for name, dtype, values in (("sparse_offsets.npy", "<i8", offsets), ("sparse_indices.npy", "<i4", indices),
                                ("sparse_values.npy", "<f4", weights)):
        out = array(path / name, dtype, (len(values),))
        out[:] = values
        out.flush()
    v.flush()
    records.flush()
    return finish(path, identity, len(rows), generator="public-fixture-v1")


def generate(path, *, count=5_000_000, dimension, model, metric="cosine", seed=447020, chunk_size=4096):
    """PCG64 streams with row-based operations independent of chunk boundaries."""
    identity = {"provider": "synthetic", "model": model, "dimension": dimension,
                "metric": metric, "generation": "synthetic-v1"}
    identity_check(identity)
    if type(count) is not int or count < 100 or chunk_size < 1 or seed < 0:
        raise ValueError("invalid generator size/seed")
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    vectors = array(path / "vectors.npy", "<f4", (count, dimension))
    records = array(path / "records.npy", RECORD, (count,))
    offsets = array(path / "sparse_offsets.npy", "<i8", (count + 1,))
    terms = array(path / "sparse_indices.npy", "<i4", (count * 3,))
    weights = array(path / "sparse_values.npy", "<f4", (count * 3,))
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, 1])))
    centers = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, 2]))).normal(size=(16, dimension))
    centers /= np.sqrt(np.sum(centers * centers, axis=1))[:, None]
    clustered, diffuse = count * 70 // 100, count * 20 // 100
    half, three_quarters = count // 2, count * 3 // 4
    for start in range(0, count, chunk_size):
        end = min(start + chunk_size, count)
        ids = np.arange(start, end, dtype=np.int64)
        noise = rng.normal(size=(len(ids), dimension))
        v = noise.copy()
        mask = ids < clustered
        v[mask] = centers[ids[mask] % 16] + noise[mask] * 0.05
        mask = ids >= clustered + diffuse
        v[mask] = centers[ids[mask] % 16]
        v /= np.sqrt(np.sum(v * v, axis=1))[:, None]
        vectors[start:end] = v.astype("<f4")
        tenant = np.where(ids < half, 0, np.where(ids < three_quarters, 1 + (ids - half) % 7,
                                               8 + (ids - three_quarters) % 56))
        offset = np.where(ids < half, ids, np.where(ids < three_quarters, ids - half, ids - three_quarters))
        width = np.where(ids < half, 1, np.where(ids < three_quarters, 7, 56))
        records["id"][start:end] = np.char.add("s", np.char.mod("%016x", ids))
        records["tenant"][start:end] = np.char.mod("t%02d", tenant)
        records["project"][start:end] = np.char.mod("p%d", (offset // width) % 4)
        records["ordinal"][start:end] = offset // (width * 4)
        records["kind"][start:end] = ids % 16
        records["time"][start:end] = ids
        records["deleted"][start:end] = ids % 997 == 996
        records["has_sparse"][start:end] = True
        sparse = np.stack((ids % 31, (ids + 1) % 31, (ids + 17) % 31), axis=1)
        order = np.argsort(sparse, axis=1)
        terms[start * 3:end * 3] = np.take_along_axis(sparse, order, axis=1).reshape(-1)
        weights[start * 3:end * 3] = np.take_along_axis(np.broadcast_to([1, 2, .5], sparse.shape), order, axis=1).reshape(-1)
        offsets[start:end] = ids * 3
    offsets[count] = count * 3
    for a in (vectors, records, offsets, terms, weights):
        a.flush()
    return finish(path, identity, count, seed=seed, generator="pcg64-row-v1", numpy=np.__version__,
                  generator_sha256=digest(__file__),
                  distribution={"clustered": clustered, "diffuse": diffuse, "ties": count - clustered - diffuse})


def load(path):
    path = Path(path)
    m = json.loads((path / "manifest.json").read_text())
    if m["schema"] != "cortex-b01-corpus-v1" or m["dataset"] != "synthetic" or set(m["files"]) != set(FILES):
        raise ValueError("unapproved corpus; real custody is separate")
    identity_check(m["identity"])
    if any(digest(path / f) != m["files"][f] for f in FILES):
        raise ValueError("corpus hash drift")
    v, r, o, i, w = [np.load(path / f, mmap_mode="r", allow_pickle=False) for f in FILES]
    n = m["count"]
    if (v.shape != (n, m["identity"]["dimension"]) or v.dtype != np.dtype("<f4") or r.shape != (n,) or r.dtype != RECORD
            or o.shape != (n + 1,) or o.dtype != np.dtype("<i8") or i.dtype != np.dtype("<i4") or w.dtype != np.dtype("<f4")
            or o[0] != 0 or o[-1] != len(i) or len(i) != len(w) or np.any(np.diff(o) < 0)
            or np.any(i < 0) or np.any(w <= 0) or not np.isfinite(w).all()):
        raise ValueError("invalid canonical arrays")
    if len(np.unique(r["id"])) != n or any(np.any(r[k] == b"") for k in ("id", "tenant", "project")):
        raise ValueError("duplicate or missing IDs/scope")
    for start in range(0, n, 4096):
        vector_check(v[start:start + 4096], m["identity"])
    if len(i) > 1:
        boundaries = np.zeros(len(i), dtype=bool)
        boundaries[o[:-1][o[:-1] < len(i)]] = True
        if np.any((i[1:] <= i[:-1]) & ~boundaries[1:]):
            raise ValueError("unsorted/duplicate sparse terms")
    return Corpus(path, m, v, r, o, i, w)


def queries(c, split):
    if split not in ("tuning", "heldout"):
        raise ValueError("unknown split")
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([c.manifest.get("seed", 447020),
                                                                  3 if split == "tuning" else 4])))
    scopes = []
    for tenant in ("t00", "t01", "t07", "t63"):
        for project in ("p0", "p1", "p2", "p3"):
            idx = np.flatnonzero(~c.records["deleted"] & (c.records["tenant"] == tenant.encode())
                                & (c.records["project"] == project.encode()))
            if len(idx):
                scopes.append((tenant, project, np.sort(c.records["ordinal"][idx])))
    if not scopes:
        raise ValueError("query mix requires synthetic scopes")
    fractions = {"broad": .25, "medium": .025, "tight": .0025, "very_tight": .00025}
    out = []
    for stratum in STRATA:
        for j in range(200):
            options = ([s for s in scopes if int(len(s[2]) * fractions[stratum]) > 11]
                       if stratum in fractions else scopes)
            tenant, project, ordinals = (options or scopes)[j % len(options or scopes)]
            n = len(ordinals)
            target = (n if stratum == "scope" else (0, 1, 9, 10, 11)[j % 5] if stratum == "edges"
                      else int(n * fractions[stratum]))
            ready = target <= n and (stratum in ("scope", "edges") or target > 11)
            first = int(rng.integers(0, max(1, n - target + 1))) if ready and target else 0
            lo = None if stratum == "scope" else int(ordinals[first]) if target and ready else -1
            hi = None if stratum == "scope" else int(ordinals[first + target - 1]) if target and ready else -1
            v = rng.normal(size=c.identity["dimension"])
            v /= np.sqrt(np.sum(v * v))
            out.append({"id": f"{split}-{stratum}-{j:03}", "split": split, "stratum": stratum,
                        "mode": "dense" if j % 10 < 3 else "hybrid" if j % 10 < 9 else "sparse",
                        "status": "READY" if ready else "NOT_RUN", "reason": None if ready else "missing-selectivity-coverage",
                        "tenant": tenant, "project": project, "lo": lo, "hi": hi, "kind": None,
                        "time_lo": None, "time_hi": None, "vector": v.astype("<f4").tolist(),
                        "sparse": [[j % 31, 1.0]], "generation": c.identity["generation"],
                        "scope_count": n, "eligible_count": target if ready else None})
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=5_000_000)
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--metric", choices=("cosine", "dot", "euclidean"), default="cosine")
    args = parser.parse_args()
    c = generate(args.output, count=args.count, dimension=args.dimension, model=args.model, metric=args.metric)
    hashes = {}
    for split in ("tuning", "heldout"):
        p = c.path / f"{split}.jsonl"
        p.write_text("".join(json.dumps(q, sort_keys=True) + "\n" for q in queries(c, split)))
        hashes[split] = digest(p)
    (c.path / "query-manifest.json").write_text(json.dumps({"corpus_manifest": digest(c.path / "manifest.json"),
                                                            "queries": hashes}, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"count": c.manifest["count"], "manifest_sha256": digest(c.path / "manifest.json"),
                      "query_hashes": hashes}, sort_keys=True))


if __name__ == "__main__":
    main()
