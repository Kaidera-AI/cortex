"""Custodian geometry boundary. Author proofs use synthetic JSONL only."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

import numpy as np
from . import corpus, oracle

DATASET = 'marlow-geometry'
UNKNOWN = ['tenant/auth', 'deletion', 'model/provider', 'semantic-query', 'sparse']
ADMISSION_FIELDS = {'schema', 'dataset', 'input_sha256', 'count', 'dimension', 'cto_decision',
                    'custody_receipt', 'import_review_sha', 'native_host_receipt', 'resize_receipt',
                    'edition', 'cache_mode', 'cost_cap_usd', 'frozen_input_sha256'}


def validate_admission(admission, dataset):
    """Validate a reviewed operator record; the operator verifies its authority."""
    if (dataset != DATASET or not isinstance(admission, dict)
            or set(admission) - ADMISSION_FIELDS
            or admission.get('schema') != 'cortex-b02-data-admission-v1'
            or admission.get('dataset') != DATASET or admission.get('count') != 141511
            or type(admission.get('count')) is not int or admission.get('dimension') != 768
            or type(admission.get('dimension')) is not int
            or not re.fullmatch('[0-9a-f]{64}', str(admission.get('input_sha256', '')))
            or not re.fullmatch('[0-9a-f]{40}', str(admission.get('import_review_sha', '')))
            or any(not isinstance(admission.get(k), str) or not admission[k].strip()
                   for k in ('cto_decision', 'custody_receipt'))):
        raise ValueError('real geometry needs shape/hash/review/CTO/custody admission before input access')


def admit_artifact(manifest, admission):
    validate_admission(admission, manifest.get('dataset'))
    geometry = manifest.get('geometry', {})
    if (geometry.get('input_sha256') != admission['input_sha256']
            or geometry.get('input_count') != 141511
            or manifest.get('identity', {}).get('dimension') != 768):
        raise ValueError('geometry admission does not bind this artifact')


def admit_native(manifest, admission):
    admit_artifact(manifest, admission)
    if (any(not isinstance(admission.get(k), str) or not admission[k].strip()
            for k in ('native_host_receipt', 'resize_receipt'))
            or admission.get('edition') not in ('linux', 'mac')
            or admission.get('cache_mode') not in ('cold', 'warm')
            or (admission['edition'] == 'mac' and admission['cache_mode'] != 'warm')
            or type(admission.get('cost_cap_usd')) not in (int, float)
            or not math.isfinite(admission['cost_cap_usd']) or not 0 < admission['cost_cap_usd'] <= 3
            or not re.fullmatch('[0-9a-f]{64}', str(admission.get('frozen_input_sha256', '')))):
        raise ValueError('native host/resize/cache/edition/cost admission missing')


def validate_row(row, dimension):
    if not isinstance(row, dict) or set(row) != {'id', 'project', 'type', 'month', 'vector'}:
        raise ValueError('exact geometry fields required; text/provenance is forbidden')
    if not isinstance(row['id'], str) or not re.fullmatch('[0-9a-f]{64}', row['id']):
        raise ValueError('pseudonymous comparison ID required')
    if row['project'] is not None and (not isinstance(row['project'], str)
                                     or not re.fullmatch('[0-9a-f]{64}', row['project'])):
        raise ValueError('project must be pseudonym or null')
    if row['type'] is not None and (not isinstance(row['type'], str) or not row['type'].strip()
                                  or len(row['type']) > 64 or any(ord(c) < 32 for c in row['type'])):
        raise ValueError('bounded coarse type or null required')
    month = row['month']
    if month is not None and (not isinstance(month, str) or not re.fullmatch('[0-9]{4}-[0-9]{2}', month)
                              or not 1 <= int(month[:4]) <= 9999 or not 1 <= int(month[5:]) <= 12):
        raise ValueError('coarse YYYY-MM or null required')
    vector = row['vector']
    if (not isinstance(vector, list) or len(vector) != dimension
            or any(type(v) not in (int, float) for v in vector)):
        raise ValueError('exactly one numeric vector required')
    with np.errstate(over='ignore', under='ignore', invalid='ignore'):
        stored = np.asarray(vector, dtype='<f4')
    norm = np.linalg.norm(stored.astype(np.float64))
    if not np.isfinite(stored).all() or not np.isfinite(norm) or norm < np.finfo(np.float32).tiny:
        raise ValueError('finite normal-float32 cosine geometry required')
    return {k: row[k] for k in ('id', 'project', 'type', 'month')}, stored


def split_rows(rows, seed):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['project'], row['type'], row['month'])].append(row)
    tuning, heldout, coverage = [], [], []
    for category in sorted(groups, key=lambda k: json.dumps(k)):
        group = sorted(groups[category], key=lambda r: (hashlib.sha256(f'{seed}:{r["id"]}'.encode()).digest(), r['id']))
        ready = len(group) >= 3
        if ready:
            tuning.append(group[0]['id'])
            heldout.append(group[1]['id'])
        coverage.append({'category': list(category), 'input': len(group), 'tuning': int(ready),
                         'heldout': int(ready), 'corpus': len(group) - 2 * int(ready),
                         'status': 'READY' if ready else 'NOT_RUN'})
    return {'tuning': sorted(tuning), 'heldout': sorted(heldout)}, coverage


def build_queries(c, rows, vectors, split, selected):
    by_id = {r['id']: r for r in rows}
    fractions = {'broad': .25, 'medium': .025, 'tight': .0025, 'very_tight': .00025}
    queries = []
    for stratum in corpus.STRATA:
        for index, sid in enumerate(selected):
            row = by_id[sid]; project = row['project'] or 'null-project'
            ordinals = c.records['ordinal'][c.records['project'] == project.encode()]
            n = len(ordinals)
            target = n if stratum == 'scope' else (0, 1, 9, 10, 11)[index % 5] if stratum == 'edges' else int(n * fractions[stratum])
            ready = target <= n and (stratum in ('scope', 'edges') or target > 11)
            lo = None if stratum == 'scope' else int(ordinals[0]) if ready and target else -1
            hi = None if stratum == 'scope' else int(ordinals[target - 1]) if ready and target else -1
            q = {'id': f'{split}-{stratum}-{sid}', 'source_id': sid, 'split': split, 'stratum': stratum,
                 'mode': 'dense', 'status': 'READY' if ready else 'NOT_RUN',
                 'reason': None if ready else 'missing-selectivity-coverage', 'tenant': 'geometry', 'project': project,
                 'lo': lo, 'hi': hi, 'kind': None, 'time_lo': None, 'time_hi': None,
                 'vector': vectors[row['offset']].tolist(), 'sparse': None, 'generation': c.identity['generation'],
                 'scope_count': n, 'eligible_count': target if ready else None,
                 'source_category': [row['project'], row['type'], row['month']]}
            if ready and oracle.rank(c, q, modes=()).eligible_count != target:
                raise ValueError('geometry predicate cardinality drift')
            queries.append(q)
    return queries


def marginal_coverage(joint):
    result = {}
    for index, field in enumerate(('project', 'type', 'month')):
        grouped = {}
        for category in joint:
            value = category['category'][index]
            counts = grouped.setdefault(value, {name: 0 for name in ('input', 'corpus', 'tuning', 'heldout')})
            for name in counts:
                counts[name] += category[name]
        result[field] = [{'value': value, **grouped[value],
                          'status': 'READY' if grouped[value]['heldout'] else 'NOT_RUN'}
                         for value in sorted(grouped, key=json.dumps)]
    return result


def vector_digest(vector):
    return hashlib.sha256(np.asarray(vector, dtype='<f4').tobytes()).hexdigest()


def freeze_input(c, rows, vectors, split):
    """Produce the import-review anchor before any engine evaluation."""
    reserved = set(split['tuning']) | set(split['heldout'])
    sources = {row['id']: vector_digest(vectors[row['offset']]) for row in rows
               if row['id'] in reserved}
    queries = {}
    for name in ('tuning', 'heldout'):
        path = c.path / (name + '.jsonl')
        values = [json.loads(line) for line in path.read_text().splitlines()]
        ordered = []
        for query in values:
            digest = vector_digest(query['vector'])
            if query['source_id'] not in split[name] or sources[query['source_id']] != digest:
                raise ValueError('generated query differs from frozen source geometry')
            ordered.append({'id': query['id'], 'source_id': query['source_id'], 'vector_sha256': digest})
        queries[name] = {'sha256': corpus.digest(path), 'count': len(values), 'ordered': ordered}
    record = {'schema': 'cortex-b02-frozen-input-v1', 'input_sha256': c.manifest['geometry']['input_sha256'],
              'input_count': c.manifest['geometry']['input_count'], 'identity': c.identity,
              'split': split, 'candidate_count': len(c.records), 'candidate_files': c.manifest['files'],
              'manifest_sha256': corpus.digest(c.path / 'manifest.json'),
              'query_manifest_sha256': corpus.digest(c.path / 'query-manifest.json'),
              'source_vectors': sources, 'queries': queries}
    target = c.path / 'frozen-input.json'
    target.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    return corpus.digest(target)


def validate_frozen(c, expected):
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('independently retained import-review SHA256 required')
    path = c.path / 'frozen-input.json'
    if corpus.digest(path) != expected:
        raise ValueError('frozen input review anchor differs')
    record = json.loads(path.read_text())
    if (record.get('schema') != 'cortex-b02-frozen-input-v1'
            or record['input_sha256'] != c.manifest['geometry']['input_sha256']
            or record['input_count'] != c.manifest['geometry']['input_count']
            or record['identity'] != c.identity or record['candidate_count'] != len(c.records)
            or record['split'] != c.manifest['geometry']['split']
            or record['candidate_files'] != c.manifest['files']
            or record['manifest_sha256'] != corpus.digest(c.path / 'manifest.json')
            or record['query_manifest_sha256'] != corpus.digest(c.path / 'query-manifest.json')):
        raise ValueError('frozen corpus/split/count/identity binding differs')
    for name in ('tuning', 'heldout'):
        target = c.path / (name + '.jsonl')
        if record['queries'][name]['sha256'] != corpus.digest(target):
            raise ValueError('frozen query bytes differ')
        values = [json.loads(line) for line in target.read_text().splitlines()]
        ordered = []
        for query in values:
            digest = vector_digest(query['vector'])
            if (query['source_id'] not in record['split'][name]
                    or record['source_vectors'].get(query['source_id']) != digest
                    or query['split'] != name or query['generation'] != c.identity['generation']):
                raise ValueError('query source ID/vector is not in the frozen split')
            ordered.append({'id': query['id'], 'source_id': query['source_id'], 'vector_sha256': digest})
        if record['queries'][name]['count'] != len(values) or record['queries'][name]['ordered'] != ordered:
            raise ValueError('frozen query count/order differs')


def prepare(source, output, *, dataset='synthetic', dimension=768, expected_count=None, seed=447020, admission=None):
    if dataset != 'synthetic':
        validate_admission(admission, dataset)  # BEFORE opening source or creating staging.
        if dimension != 768 or expected_count not in (None, 141511):
            raise ValueError('real shape differs from admitted Marlow geometry')
        expected_count = 141511
    if (type(dimension) is not int or not 1 <= dimension <= 2000 or type(seed) is not int or seed < 0
            or expected_count is not None and (type(expected_count) is not int or expected_count < 1)):
        raise ValueError('explicit dimension/count/seed required')
    source, output = Path(source), Path(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError('fresh destination with existing parent required')
    with tempfile.TemporaryDirectory(prefix='.b02-geometry-', dir=output.parent) as scratch:
        temporary = Path(scratch); rows, ids = [], set(); source_hash = hashlib.sha256()
        with source.open('rb') as incoming, (temporary / 'stored.f32').open('wb') as stored:
            for line in incoming:
                source_hash.update(line)
                try:
                    row, vector = validate_row(json.loads(line), dimension)
                except (TypeError, json.JSONDecodeError, OverflowError) as error:
                    raise ValueError('invalid geometry row') from error
                if row['id'] in ids:
                    raise ValueError('duplicate comparison ID')
                ids.add(row['id']); row['offset'] = len(rows); rows.append(row); stored.write(vector.tobytes())
        input_hash = source_hash.hexdigest()
        if not rows or expected_count is not None and len(rows) != expected_count:
            raise ValueError('input count differs from frozen admission')
        if dataset != 'synthetic' and input_hash != admission['input_sha256']:
            raise ValueError('custodian input hash drift')
        rows.sort(key=lambda r: r['id'])
        vectors = np.memmap(temporary / 'stored.f32', mode='r', dtype='<f4', shape=(len(rows), dimension))
        canonical = hashlib.sha256(f'geometry-stored-f32:{dimension}:cosine'.encode())
        for row in rows:
            canonical.update(json.dumps([row[k] for k in ('id', 'project', 'type', 'month')], separators=(',', ':')).encode())
            canonical.update(vectors[row['offset']].tobytes())
        identity = {'provider': 'unknown', 'model': 'unknown', 'dimension': dimension,
                    'metric': 'cosine', 'generation': 'geometry-' + canonical.hexdigest()}
        split, coverage = split_rows(rows, seed)
        excluded = set(split['tuning']) | set(split['heldout'])
        candidates = [r for r in rows if r['id'] not in excluded]
        types = sorted({r['type'] for r in rows if r['type'] is not None})
        codes = {value: i + 1 for i, value in enumerate(types)}
        built = temporary / 'artifact'; built.mkdir()
        v = corpus.array(built / 'vectors.npy', '<f4', (len(candidates), dimension))
        records = corpus.array(built / 'records.npy', corpus.RECORD, (len(candidates),))
        ordinals = defaultdict(int)
        for i, row in enumerate(candidates):
            project = row['project'] or 'null-project'
            v[i] = vectors[row['offset']]
            records[i] = (row['id'].encode(), b'geometry', project.encode(), codes.get(row['type'], 0),
                          int(row['month'].replace('-', '')) if row['month'] is not None else -1,
                          ordinals[project], False, False)
            ordinals[project] += 1
        v.flush(); records.flush()
        for name, dtype, count in [('sparse_offsets.npy', '<i8', len(candidates)+1),
                                   ('sparse_indices.npy', '<i4', 0), ('sparse_values.npy', '<f4', 0)]:
            a = corpus.array(built / name, dtype, (count,)); a[:] = 0; a.flush()
        geometry = {'schema': 'cortex-b02-geometry-v1', 'qualification': 'GEOMETRY_ONLY', 'unknown': UNKNOWN,
                    'input_sha256': input_hash, 'input_count': len(rows), 'canonical_sha256': canonical.hexdigest(),
                    'seed': seed, 'split_rule': 'sha256-seed-id-joint-category-reserve-one-each-v1',
                    'split': split, 'coverage': coverage, 'marginal_coverage': marginal_coverage(coverage),
                    'kind_codes': [{'code': 0, 'value': None}]
                    + [{'code': codes[t], 'value': t} for t in types],
                    'project_null': 'null-project', 'month_encoding': 'YYYYMM; null=-1',
                    'ordinal': 'candidate ID order within project', 'source_sha256': corpus.digest(__file__)}
        c = corpus.finish(built, identity, len(candidates), generator='marlow-geometry-split-v1', geometry=geometry)
        if dataset != 'synthetic':
            c.manifest['dataset'] = dataset
            (built / 'manifest.json').write_text(json.dumps(c.manifest, sort_keys=True, indent=2)+'\n')
        hashes = {}
        for name in ('tuning', 'heldout'):
            target = built / (name + '.jsonl')
            target.write_text(''.join(json.dumps(q, sort_keys=True, allow_nan=False)+'\n'
                                      for q in build_queries(c, rows, vectors, name, split[name])))
            hashes[name] = corpus.digest(target)
        (built / 'query-manifest.json').write_text(json.dumps({'corpus_manifest': corpus.digest(built/'manifest.json'),
                                                               'queries': hashes}, sort_keys=True, indent=2)+'\n')
        frozen_input_sha256 = freeze_input(c, rows, vectors, split)
        built.rename(output)
    return load(output, admission=admission, frozen_input_sha256=frozen_input_sha256)


def load(path, *, admission=None, frozen_input_sha256=None):
    c = corpus.load(path) if admission is None else corpus.load(path, geometry_admission=admission)
    geometry = c.manifest.get('geometry', {})
    if geometry.get('schema') != 'cortex-b02-geometry-v1' or geometry.get('unknown') != UNKNOWN:
        raise ValueError('geometry qualification missing')
    split = geometry['split']; tuning, heldout = set(split['tuning']), set(split['heldout'])
    ids = {x.decode() for x in c.records['id']}
    if (tuning & heldout or ids & (tuning | heldout) or len(tuning) != len(split['tuning'])
            or len(heldout) != len(split['heldout']) or len(ids)+len(tuning)+len(heldout) != geometry['input_count']):
        raise ValueError('geometry split leakage or inventory drift')
    binding = json.loads((c.path/'query-manifest.json').read_text())
    if binding['corpus_manifest'] != corpus.digest(c.path/'manifest.json') or set(binding['queries']) != {'tuning', 'heldout'}:
        raise ValueError('geometry corpus/query binding drift')
    for name in ('tuning', 'heldout'):
        target = c.path / (name+'.jsonl')
        if binding['queries'][name] != corpus.digest(target):
            raise ValueError('geometry query hash drift')
    validate_frozen(c, frozen_input_sha256)
    c.frozen_input_sha256 = frozen_input_sha256
    return c


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('output', type=Path)
    parser.add_argument('--dataset', choices=('synthetic', DATASET), default='synthetic')
    parser.add_argument('--dimension', type=int, default=768); parser.add_argument('--expected-count', type=int)
    parser.add_argument('--admission', type=Path)
    args = parser.parse_args()
    admission = json.loads(args.admission.read_text()) if args.admission else None
    c = prepare(args.source, args.output, dataset=args.dataset, dimension=args.dimension,
                expected_count=args.expected_count, admission=admission)
    print(json.dumps({'dataset': c.manifest['dataset'], 'count': c.manifest['count'],
                      'manifest_sha256': corpus.digest(c.path/'manifest.json'),
                      'frozen_input_sha256': c.frozen_input_sha256, 'qualification': 'GEOMETRY_ONLY'}))


if __name__ == '__main__':
    main()
