"""C05 copied-source semantic faults; dependency failures never count as kills."""
from pathlib import Path
import re
import sys

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT/'tests'))
sys.path.insert(0, str(NEXT/'scripts'))
from test_receipts import classify, report, suite


def mutation_status(result, expected):
    value = report(result)
    if value is not None and any(re.search(r'(?:^|\n)(?:ImportError|ModuleNotFoundError):', row['traceback'])
                                 for row in value['failures']):
        return 'inconclusive'
    return classify(result, expected)


import hashlib
import json
import os
import psycopg
from mutate_core_adapters import capture

RECIPES = NEXT/'contracts/c05-rework-fault-recipes.json'
MANIFEST = 'schema/manifest.json'
DIRECTORIES = ('c05_review','c05_private')


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as connection:
        connection.execute('CHECKPOINT')


def clean(result):
    return result['exit_code']==0 and result['receipt'] is not None and not result['receipt']['failures'] and not result['receipt']['errors']


MATRICES = {
    'review': (RECIPES, DIRECTORIES),
    'adapters': (NEXT/'contracts/c05-adapters-fault-recipes.json', ('records','coordination','core_adapters','acceptance_guards')),
}


def run(matrix='review'):
    recipes_path,directories=MATRICES[matrix]
    mutations=json.loads(recipes_path.read_bytes())['mutants']
    paths=sorted({edit['path'] for row in mutations for edit in row['changes']} | {MANIFEST})
    originals={name:(NEXT/name).read_bytes() for name in paths}
    hashes={name:hashlib.sha256(body).hexdigest() for name,body in originals.items()}
    # Validate every recipe before any fault. Missing anchors are operational failures.
    for recipe in mutations:
        sources={name:body.decode() for name,body in originals.items()}
        for edit in recipe['changes']:
            assert sources[edit['path']].count(edit['before'])==edit['count'],(recipe['label'],edit['path'])
            sources[edit['path']]=sources[edit['path']].replace(edit['before'],edit['after'])
    baseline={directory:capture(suite(NEXT/'tests'/directory)) for directory in directories}
    print(json.dumps({'baseline':baseline,'recipes_sha256':hashlib.sha256(recipes_path.read_bytes()).hexdigest()}),flush=True)
    if not all(clean(value) for value in baseline.values()):raise SystemExit('C05 baseline is RED')
    def apply(changes):
        changed=set()
        for edit in changes:
            path=NEXT/edit['path'];source=path.read_text()
            assert source.count(edit['before'])==edit['count'],'mutation anchor'
            path.write_text(source.replace(edit['before'],edit['after']));changed.add(edit['path'])
        if any(name.endswith('.sql') for name in changed):
            manifest=json.loads(originals[MANIFEST])
            for entry in manifest['migrations']:
                if 'schema/'+entry['file'] in changed:
                    entry['sha256']=hashlib.sha256((NEXT/'schema'/entry['file']).read_bytes()).hexdigest()
            (NEXT/MANIFEST).write_text(json.dumps(manifest,indent=2)+'\n')
        return changed

    rows=[]
    try:
        for recipe in mutations:
            checkpoint();changed=set()
            try:
                expected=set(recipe['expected_tests'])
                auxiliary=None
                original_count=recipe.get('original_change_count',len(recipe['changes']))
                if original_count<len(recipe['changes']):
                    apply(recipe['changes'][original_count:])
                    control=suite(NEXT/'tests'/recipe['directory']);r=report(control)
                    failed=set() if r is None else {f['id'].split(' (')[0] for f in r['failures']}
                    valid=r is not None and not r['errors'] and not expected.intersection(failed)
                    valid=valid and control.returncode in (0,1) and all(f['phase']=='test' and f['is_assertion'] for f in r['failures'])
                    valid=valid and clean(capture(control))
                    auxiliary=dict(qualified=valid,expected_boundary_unchanged=not expected.intersection(failed),
                        effective_source_sha256={name:hashlib.sha256((NEXT/name).read_bytes()).hexdigest() for name in paths},**capture(control))
                    for name,body in originals.items():(NEXT/name).write_bytes(body)
                    checkpoint()
                changed=apply(recipe['changes'])
                result=suite(NEXT/'tests'/recipe['directory']);expected=set(recipe['expected_tests'])
                status=mutation_status(result,expected);value=report(result)
                failed=set() if value is None else {f['id'].split(' (')[0] for f in value['failures']}
                if status=='killed' and (not expected<=failed or (auxiliary is not None and not auxiliary['qualified'])):status='inconclusive'
                row=dict(source=sorted(changed),mutation=recipe['label'],recipe=recipe['changes'],
                    expected_test=recipe['expected_tests'][0],expected_tests=recipe['expected_tests'],kind=recipe['kind'],status=status,auxiliary_control=auxiliary,
                    original_source_sha256=hashes,effective_source_sha256={name:hashlib.sha256((NEXT/name).read_bytes()).hexdigest() for name in paths},**capture(result))
                rows.append(row);print(json.dumps(row),flush=True)
            finally:
                for name,body in originals.items():(NEXT/name).write_bytes(body)
    finally:
        for name,body in originals.items():(NEXT/name).write_bytes(body)
    checkpoint()
    restored={directory:capture(suite(NEXT/'tests'/directory)) for directory in directories}
    final={name:hashlib.sha256((NEXT/name).read_bytes()).hexdigest() for name in paths}
    summary=dict(mutants=len(rows),killed=sum(row['status']=='killed' for row in rows),
        survivors=[r['mutation'] for r in rows if r['status']=='survived'],
        inconclusive=[r['mutation'] for r in rows if r['status']=='inconclusive'],restored_source_sha256=final,restored=restored)
    print(json.dumps(summary),flush=True)
    if len(rows)!=len(mutations) or any(row['status']!='killed' for row in rows):raise SystemExit('C05 mutation qualification incomplete')
    if hashes!=final or not all(clean(value) for value in restored.values()):raise SystemExit('C05 source/baseline restoration failed')


if __name__=='__main__':
    run('adapters' if '--adapters' in sys.argv else 'review')
