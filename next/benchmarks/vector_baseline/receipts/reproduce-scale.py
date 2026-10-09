import json,time,shutil,sys
from pathlib import Path
import numpy as np
from vector_baseline import corpus,oracle
root=Path.cwd()/"next/benchmarks/vector_baseline"
data=root/"data";receipts=root/"receipts"
start=time.monotonic()
previous=None
for name in ("scale-8","scale-8-repeat","sql-smoke"):
    p=data/name
    if p.exists():
        manifest=json.loads((p/"manifest.json").read_text())
        assert manifest["dataset"]=="synthetic" and manifest["identity"]["dimension"]==8
        if name=="scale-8":previous=manifest["files"]
        shutil.rmtree(p)
first=corpus.generate(data/"scale-8",dimension=8,model="b01-synthetic-fixture-8")
second=corpus.generate(data/"scale-8-repeat",dimension=8,model="b01-synthetic-fixture-8",chunk_size=8192)
assert first.manifest["count"]==second.manifest["count"]==5000000
assert first.manifest["files"]==second.manifest["files"]
assert previous is None or previous==first.manifest["files"]
hashes={}
queries={}
for split in ("tuning","heldout"):
    q=corpus.queries(first,split);queries[split]=q
    path=first.path/(split+".jsonl")
    path.write_text("".join(json.dumps(row,sort_keys=True)+"\n" for row in q))
    hashes[split]=corpus.digest(path)
manifest={"corpus_manifest":corpus.digest(first.path/"manifest.json"),"queries":hashes}
(first.path/"query-manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n")
q=queries["heldout"];assert all(row["status"]=="READY" for row in q)
assert not {row["id"] for row in q}&{row["id"] for row in queries["tuning"]}
mix={s:sum(row["stratum"]==s for row in q) for s in corpus.STRATA}
edge={m:sorted({row["eligible_count"] for row in q if row["stratum"]=="edges" and row["mode"]==m}) for m in ("dense","hybrid","sparse")}
assert all(v==[0,1,9,10,11] for v in edge.values())
probes=[]
for stratum in corpus.STRATA:
    row=next(x for x in q if x["stratum"]==stratum)
    records=first.records
    scope=(records["tenant"]==row["tenant"].encode())&(records["project"]==row["project"].encode())&~records["deleted"]
    mask=scope.copy()
    if row["lo"] is not None:mask&=records["ordinal"]>=row["lo"]
    if row["hi"] is not None:mask&=records["ordinal"]<=row["hi"]
    n=int(np.count_nonzero(mask));total=int(np.count_nonzero(scope))
    assert n==row["eligible_count"] and total==row["scope_count"]
    assert oracle.rank(first,row,modes=()).eligible_count==n
    probes.append({"stratum":stratum,"eligible":n,"scope":total})
for stratum in ("scope","tight","very_tight"):
    row=next(x for x in q if x["stratum"]==stratum)
    truth=oracle.rank(first,row)
    probes.append({"stratum":stratum,"exhaustive_corpus_count":5000000,"eligible":truth.eligible_count,
                   "top10_public_fixture_ids":{m:truth.ids(m,limit=10) for m in ("dense","sparse","hybrid")}})
for source,target in ((first.path/"manifest.json","scale-manifest.json"),(first.path/"query-manifest.json","scale-query-manifest.json")):
    shutil.copyfile(source,receipts/target)
result={"count":5000000,"dimension":8,"identity":"synthetic fixture only",
        "generator_sha256":first.manifest["generator_sha256"],"files_identical":True,
        "same_arrays_as_prior_source":previous is not None,"first_chunk_size":4096,"second_chunk_size":8192,
        "all_heldout_ready":True,"query_mix":mix,"edge_cardinalities_per_mode":edge,
        "oracle_probes":probes,"elapsed_seconds":time.monotonic()-start,"native_product_benchmark":"NOT_RUN"}
(receipts/"scale-reproduction.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps({"count":5000000,"manifest_sha256":corpus.digest(first.path/"manifest.json"),"query_hashes":hashes,"files_identical":True}),flush=True)
# Prepare fresh same-source small SQL fixture; actual SQL runner is separate.
small=corpus.generate(data/"sql-smoke",count=24000,dimension=8,model="b01-synthetic-fixture-8")
qs=corpus.queries(small,"heldout");path=small.path/"heldout.jsonl"
path.write_text("".join(json.dumps(row,sort_keys=True)+"\n" for row in qs))
(small.path/"query-manifest.json").write_text(json.dumps({"corpus_manifest":corpus.digest(small.path/"manifest.json"),"queries":{"heldout":corpus.digest(path)}},sort_keys=True,indent=2)+"\n")
