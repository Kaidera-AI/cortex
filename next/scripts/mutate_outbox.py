"""C06 copied-source semantic faults; dependency failures never count as kills."""
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
from mutate_identity import capture
from verify_writer_inventory import scan as writer_scan

RECIPES = NEXT/'contracts/outbox-fault-recipes.json'
MUTATIONS = json.loads(RECIPES.read_bytes())['mutants']
MANIFEST = 'schema/manifest.json'
INVENTORY = 'contracts/core-writer-inventory.json'
DIRECTORIES = ('outbox','outbox_guards','outbox_retention','outbox_inventory','outbox_late_publication','outbox_caller')


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as connection:
        connection.execute('CHECKPOINT')


def clean(result):
    return result['exit_code']==0 and result['receipt'] is not None and not result['receipt']['failures'] and not result['receipt']['errors']


MATRICES = {
    'outbox': (RECIPES, DIRECTORIES),
    'identity': (NEXT/'contracts/outbox-identity-fault-recipes.json', ('auth_identity','auth_identity_guards','identity_coordination','identity_portability','identity_receipts','identity_transition','identity_policy_faults','identity_binding')),
    'adapters': (NEXT/'contracts/outbox-adapters-fault-recipes.json', ('records','coordination','core_adapters','acceptance_guards')),
    'c05': (NEXT/'contracts/outbox-c05-fault-recipes.json', ('c05_review','c05_private')),
}


def run(matrix='outbox', repair_only=False, attribution_only=False):
    recipes_path,directories=MATRICES[matrix]
    mutations=json.loads(recipes_path.read_bytes())['mutants']
    if repair_only:
        selection=json.loads((NEXT/'contracts/outbox-matrix-repair-selection.json').read_bytes())[matrix]
        mutations=[row for row in mutations if row['label'] in selection]
        assert len(mutations)==len(selection), 'repair selection must name exact failed faults'
    if attribution_only:
        mutations=[row for row in mutations if row['label']=='private coordination schema usage removed']
        assert len(mutations)==1
    paths=sorted({edit['path'] for row in mutations for edit in row['changes']} | {MANIFEST,INVENTORY})
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
    if not all(clean(value) for value in baseline.values()):raise SystemExit('C06 baseline is RED')
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
        if INVENTORY not in changed and any(name.endswith('.sql') or name.startswith('src/') for name in changed):
            inventory=json.loads(originals[INVENTORY]);inventory['writer_sites']=writer_scan(NEXT)
            inventory['unclassified_writers']=[r for r in inventory['writer_sites'] if r['classification'] is None]
            (NEXT/INVENTORY).write_text(json.dumps(inventory,indent=2)+'\n')
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
                    if recipe['kind']=='frozen_predecessor_semantic_fault':valid=valid and clean(capture(control))
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
    if len(rows)!=len(mutations) or any(row['status']!='killed' for row in rows):raise SystemExit('C06 mutation qualification incomplete')
    if hashes!=final or not all(clean(value) for value in restored.values()):raise SystemExit('C06 source/baseline restoration failed')


if __name__=='__main__':
    run(next((key for key in MATRICES if '--'+key in sys.argv),'outbox'), repair_only='--repair' in sys.argv, attribution_only='--attribution-red' in sys.argv)
