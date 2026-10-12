"""Literal faults through real frozen tests; existing admitted fixture supplies PG.
No allocator, provider request, fixture weakening or inferred mutation kill.
"""
from pathlib import Path
import hashlib,json,os,subprocess,sys

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'next/src/cortex_core/embeddings/pg_search.py'
PRIMARY='next/tests/integration/test_pg_search_kind_filter.py'
EXTRA='next/tests/integration/test_pg_search_kind_filter_hnsw.py'


def main():
    if os.environ.get('NEMO_KIND_FILTER_OWNED_FIXTURE')!='1':
        raise RuntimeError('existing owned disposable-PG fixture authority required')
    directory=Path(sys.argv[1]).resolve(strict=True)
    raw=SOURCE.read_bytes();text=raw.decode();tests={path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in [PRIMARY,EXTRA]}
    begin=text.index('        if kind is not None and (');end=text.index('        key = identity.key',begin)
    validation=text[begin:end]
    native=PRIMARY+'::TestNativeKindFilter::';companion=EXTRA+'::TestHnswCompanion::'
    recipes=[
        ('drop-validation',validation,'',PRIMARY+'::test_offline_invalid_kind_refuses_before_any_database_effect[False]'),
        ('drop-byte-bound','or not 1 <= len(kind.encode("utf-8")) <= 64','or False',PRIMARY+'::test_offline_invalid_kind_refuses_before_any_database_effect['+'a'*65+']'),
        ('bind-unrestricted','                limit,\n                kind,','                limit,\n                None,',PRIMARY+'::test_offline_kind_is_bound_as_a_value_with_existing_scope_and_limit[memory]'),
        ('drop-kind-predicate','AND ($6::text IS NULL OR s.kind=$6)','AND ($6::text IS NULL OR TRUE)',native+'test_native_kind_predicate_precedes_top_k'),
        ('filter-default-none','AND ($6::text IS NULL OR s.kind=$6)','AND s.kind=$6',native+'test_native_default_and_explicit_none_preserve_all_kinds'),
        ('drop-requested-limit','ORDER BY v.embedding <=> $4::vector LIMIT $5','ORDER BY v.embedding <=> $4::vector LIMIT (100 + $5)',companion+'test_native_limit_applies_to_ranked_matching_kinds'),
        ('hide-pending-freshness','            freshness = await self._freshness(conn, scope, key)','            freshness = await self._freshness(conn, scope, key)\n            freshness = Freshness(key, freshness.indexed_records, 0)',native+'test_native_kind_never_hides_pending_projection_freshness'),
        ('disable-iterative-hnsw',"hnsw.iterative_scan='strict_order'","hnsw.iterative_scan='off'",companion+'test_native_real_hnsw_index_path_keeps_the_kind_restriction'),
    ]
    rows=[]
    try:
        for label,before,after,expected in recipes:
            assert text.count(before)==1,(label,text.count(before))
            mutant=text.replace(before,after,1).encode();SOURCE.write_bytes(mutant)
            report=directory/(label+'.json');env=dict(os.environ);env.update(REN_CX_REPORT=str(report),PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
            args=[sys.executable,'-B','-m','pytest','-p','pytest_receipts','-q','--tb=short','-o','cache_dir='+str(directory/'cache'),expected]
            result=subprocess.run(args,cwd=ROOT,env=env,capture_output=True,text=True,timeout=120)
            (directory/(label+'.stdout')).write_text(result.stdout);(directory/(label+'.stderr')).write_text(result.stderr)
            d=json.loads(report.read_text());failures=[r for r in d['reports'] if r['outcome']=='failed']
            valid=result.returncode==d['exit']==1 and len(failures)==1 and not d['collection_errors'] and failures[0]['phase']=='call' and failures[0]['nodeid']==expected and failures[0].get('exception_type')=='AssertionError' and failures[0].get('raised_in_expected_test_body')
            classification='KILLED_BODY' if valid else 'SURVIVED' if result.returncode==0 else 'INCONCLUSIVE'
            row={'label':label,'source_path':str(SOURCE.relative_to(ROOT)),'original_sha256':hashlib.sha256(raw).hexdigest(),'mutant_sha256':hashlib.sha256(mutant).hexdigest(),'literal_before':before,'literal_after':after,'expected_body':expected,'result':classification,'exit':result.returncode,'frozen_tests':tests}
            rows.append(row);(directory/'mutants-progress.json').write_text(json.dumps(rows,indent=2)+'\n')
            SOURCE.write_bytes(raw)
            assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest for path,digest in tests.items())
            print(label,classification,flush=True)
            if classification!='KILLED_BODY': raise RuntimeError('fault did not produce the exact expected assertion: '+label)
        (directory/'mutants.json').write_text(json.dumps(rows,indent=2)+'\n')
    finally:
        SOURCE.write_bytes(raw)
        assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest for path,digest in tests.items())
    return 0

if __name__=='__main__':raise SystemExit(main())
