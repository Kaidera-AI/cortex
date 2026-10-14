"""Inventory every next/ suite, then require complete offline per-file outcomes."""
import argparse
import ast
from collections import Counter
import errno
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / '.github/next-offline-suites.json'
SPEC = importlib.util.spec_from_file_location('unchanged_u13_outcome',
                                            Path(__file__).with_name('pytest_u13_worker.py'))
U13 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U13)


def suite_name(name):
    return name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))


def retirement_require(condition, code):
    if not condition:
        raise ValueError('retirement: ' + code)


def source_tree(path):
    try:
        return ast.parse(path.read_text()), True
    except (OSError, UnicodeError, SyntaxError):
        return ast.Module(body=[], type_ignores=[]), False


def named_nodes(path, tree):
    nodes = [str(path) + '::' + node.name for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')]
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            nodes.extend(str(path) + '::' + cls.name + '::' + node.name for node in cls.body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'))
    return sorted(nodes)


def retirement_inventory(root, run, bindings):
    """Read existing authority literals, never execute their unittest runners."""
    digest = lambda path: hashlib.sha256((root/path).read_bytes()).hexdigest() if (root/path).is_file() else None
    sha = lambda value: isinstance(value, str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
    retirement_require(isinstance(bindings, list) and all(isinstance(row, dict)
        and set(row)=={'record','sha256','files'} and isinstance(row['record'], str)
        and sha(row['sha256']) and isinstance(row['files'], dict)
        and all(isinstance(path, str) and sha(pin) for path,pin in row['files'].items())
        for row in bindings), 'binding_schema')
    records = {}
    for path in sorted((root/'next/tests').rglob('run_active.py')):
        tree, parsed = source_tree(path)
        retirement_require(parsed, 'record_parse')
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id in ('RETIRED','retired') for target in node.targets)]
        if values:
            records[str(path.relative_to(root))] = values
    retirement_require(len(bindings)==len({row['record'] for row in bindings})
        and {row['record'] for row in bindings}==set(records), 'record_inventory')
    retired, replacements = [], {}
    for binding in bindings:
        path = binding['record']
        retirement_require(digest(path)==binding['sha256'], 'record_hash')
        values = records.get(path, [])
        literal, unique = {}, False
        if len(values)==1 and isinstance(values[0], ast.Dict):
            try:
                keys = [ast.literal_eval(key) for key in values[0].keys]
                literal = ast.literal_eval(values[0])
                unique = len(keys)==len(set(keys))
            except (ValueError, TypeError):
                pass
        retirement_require(unique and isinstance(literal, dict) and bool(literal)
            and all(isinstance(key, str) and len(key.split('.'))==3
                    and all(part.isidentifier() for part in key.split('.'))
                    and key.split('.')[-1].startswith('test_') and isinstance(reason, str)
                    and bool(reason.strip()) and len(reason.splitlines())==1
                    for key,reason in literal.items()), 'record_literal')
        originals = {str(Path(path).parent/(name.split('.')[0]+'.py')) for name in literal}
        pairs = {original:original[:-3]+'_v2.py' for original in originals}
        retirement_require(set(binding['files'])==originals|set(pairs.values()), 'file_bindings')
        retirement_require(all(digest(file)==pin for file,pin in binding['files'].items()), 'file_hash')
        retirement_require(all(file in run for file in originals|set(pairs.values())), 'live_suite')
        for original,replacement in pairs.items():
            tree, parsed = source_tree(root/replacement)
            nodes = named_nodes(replacement, tree)
            retirement_require(parsed and bool(nodes), 'replacement_nodes')
            replacements[replacement] = nodes
        for name,reason in literal.items():
            module,cls,method = name.split('.')
            original = str(Path(path).parent/(module+'.py'))
            tree, parsed = source_tree(root/original)
            node = original+'::'+cls+'::'+method
            retirement_require(parsed and node in named_nodes(original, tree), 'original_node')
            retired.append(dict(nodeid=node, reason=reason, record=path,
                record_sha256=binding['sha256'], source_sha256=binding['files'].get(original),
                replacement_nodes=replacements[pairs[original]]))
    return retired, replacements


def inventory(root, manifest):
    root = Path(root).resolve()
    found = sorted(str(p.relative_to(root)) for p in (root / 'next').rglob('*.py')
                   if suite_name(p.name))
    invalid, entries, run, excluded = [], [], [], []
    # rglob does not descend into directory symlinks: reject them rather than hide suites.
    for path in (root / 'next').rglob('*'):
        if path.is_symlink():
            invalid.append('symlink under next inventory: ' + str(path.relative_to(root)))
    if not isinstance(manifest, dict) or set(manifest) not in ({'run', 'excluded'}, {'run', 'excluded', 'retirements'}):
        invalid.append('manifest must contain run/excluded and optional retirement source bindings')
        manifest = manifest if isinstance(manifest, dict) else {}
    for key in ('run', 'excluded'):
        values = manifest.get(key)
        if not isinstance(values, list):
            invalid.append(key + ' must be a list')
            continue
        for row in values:
            path = row if key == 'run' else row.get('path') if isinstance(row, dict) else None
            if (not isinstance(path, str) or not path.startswith('next/')
                    or any(c in path for c in '*?[]\\')
                    or any(part in ('', '.', '..') for part in path.split('/'))
                    or not suite_name(PurePosixPath(path).name)):
                invalid.append('invalid explicit suite path: ' + repr(path))
                continue
            entries.append(path)
            if key == 'run':
                run.append(path)
            else:
                reason = row.get('reason')
                if (set(row) != {'path', 'reason'} or not isinstance(reason, str)
                        or not reason.strip() or len(reason.splitlines()) != 1):
                    invalid.append('missing or non-single-line exclusion reason: ' + path)
                excluded.append(row)
            absolute = root / path
            if absolute.is_symlink() or not absolute.resolve().is_relative_to(root / 'next'):
                invalid.append('suite symlink or escape: ' + path)
    duplicates = sorted(path for path, count in Counter(entries).items() if count != 1)
    if not run:
        invalid.append('RUN manifest must not be empty')
    if not (root / 'next').is_dir():
        invalid.append('next source root is absent')
    missing, stale = sorted(set(found) - set(entries)), sorted(set(entries) - set(found))
    retired, replacements = [], {}
    if not invalid:
        try:
            retired, replacements = retirement_inventory(root, run, manifest.get('retirements', []))
        except ValueError as error:
            invalid.append(str(error))
    oracle = 'next/docs/runtime/rc0/test_oracle.py'
    return dict(ok=not (invalid or duplicates or missing or stale), found=found,
                run=run, excluded=excluded, missing=missing, stale=stale,
                duplicates=duplicates, invalid=invalid, rc0_oracle_present=oracle in found,
                retired=retired, replacement_nodes=replacements)


def receipt_valid(packet, process_exit, retired=(), replacements=()):
    try:
        return (process_exit == 0 and packet['gate_exit'] == 0
                and U13.valid(packet['expected'], packet)
                and packet.get('retired', []) == list(retired)
                and not (set(packet['expected']) & {row['nodeid'] for row in retired})
                and (not retired or Counter(packet.get('collected', [])) ==
                     Counter(packet['expected'] + [row['nodeid'] for row in retired]))
                and (not replacements or sorted(packet['expected']) == sorted(replacements)))
    except (KeyError, TypeError, ValueError):
        return False


def write(path, packet):
    path.write_text(json.dumps(packet, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--force-skip', action='store_true', help='deliberately RED hosted control')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checked = inventory(ROOT, json.loads(MANIFEST.read_text()))
    packet = dict(inventory=checked, suites=[], gate_exit=1,
                  manifest_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
                  python=sys.version, qualification='HOSTED_OFFLINE_SOURCE_CONTRACTS_ONLY',
                  native_qualified=False, installed_qualified=False, old_installation='G_OLD_UNRESOLVED',
                  control='forced-skip' if args.force_skip else 'normal')
    result = args.output / 'summary.json'
    write(result, packet)
    print(json.dumps(checked, indent=2), flush=True)
    if not checked['ok']:
        print('NEXT_OFFLINE_INVENTORY_RED', flush=True)
        return 1
    if args.inventory_only:
        packet['gate_exit'] = 0
        write(result, packet)
        print('NEXT_OFFLINE_INVENTORY_GREEN', flush=True)
        return 0
    denied = []
    for family in (socket.AF_INET, socket.AF_INET6):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            try:
                probe = socket.socket(family, kind)
            except OSError as error:
                denied.append(dict(family=int(family), kind=int(kind), errno=error.errno,
                                   denied=error.errno == errno.EAFNOSUPPORT))
            else:
                probe.close()
                denied.append(dict(family=int(family), kind=int(kind), denied=False))
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    packet['offline_runtime'] = dict(socket_creation_probes=denied,
        network_namespace=os.readlink('/proc/self/ns/net'), uid=os.getuid(),
        no_new_privs=status['NoNewPrivs'].strip(), ip_socket_denied=all(r['denied'] for r in denied))
    write(result, packet)
    if not packet['offline_runtime']['ip_socket_denied'] or packet['offline_runtime']['no_new_privs'] != '1':
        print('NEXT_OFFLINE_RUNTIME_RESTRICTION_RED', flush=True)
        return 1
    import psycopg
    packet['source_head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    packet['dependencies'] = {name: importlib.metadata.version(name) for name in
                              ('pytest', 'numpy', 'psycopg', 'jsonschema', 'asyncpg', 'httpx', 'starlette')}
    packet['driver'] = dict(version=psycopg.__version__, pq_impl=psycopg.pq.__impl__, libpq_version=psycopg.pq.version())
    packet['unchanged_u13_validator_sha256'] = hashlib.sha256(
        Path(__file__).with_name('pytest_u13_worker.py').read_bytes()).hexdigest()
    write(result, packet)
    worker = Path(__file__).with_name('pytest_next_worker.py')
    with tempfile.TemporaryDirectory(prefix='next-offline-') as temporary:
        case = Path(temporary)
        case.chmod(0o700)
        # Child tests receive no inherited tokens, credentials or database endpoints.
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        environment.update(HOME=str(case / 'home'), TMPDIR=str(case),
                           CORTEX_U13_CASE_ROOT=str(case), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                           PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        (case / 'home').mkdir()
        for index, suite in enumerate(checked['run']):
            retired = [row for row in checked['retired'] if row['nodeid'].split('::')[0] == suite]
            name = f'{index:03d}'
            destination = args.output / (name + '.json')
            command = [sys.executable, '-B', str(worker), '--suite', suite,
                       '--output', str(destination), '--temporary', str(case / name),
                       '--retired-json', json.dumps(retired)]
            source_sha = hashlib.sha256((ROOT / suite).read_bytes()).hexdigest()
            if args.force_skip and index == 0:
                command.append('--force-skip')
            started = time.monotonic()
            try:
                process = subprocess.run(command, cwd=ROOT, env=environment,
                                         capture_output=True, text=True, timeout=180)
                code, log = process.returncode, process.stdout + process.stderr
            except subprocess.TimeoutExpired as error:
                code, log = 124, 'SUITE_TIMEOUT\n' + str(error.stdout) + str(error.stderr)
            (args.output / (name + '.txt')).write_text(log)
            try:
                outcome = json.loads(destination.read_text())
            except (OSError, ValueError):
                outcome = None
            source_unchanged = hashlib.sha256((ROOT / suite).read_bytes()).hexdigest() == source_sha
            ok = (receipt_valid(outcome, code, retired=retired,
                                replacements=checked['replacement_nodes'].get(suite, ())) and source_unchanged
                  and outcome.get('suite') == suite and outcome.get('source_sha256') == source_sha
                  and all(node.startswith(suite + '::') for node in outcome['selected']))
            row = dict(suite=suite, source_sha256=source_sha, source_unchanged=source_unchanged,
                       process_exit=code, receipt=name + '.json', log=name + '.txt',
                       elapsed_seconds=round(time.monotonic() - started, 3), ok=ok,
                       selected=len(outcome.get('selected', [])) if outcome else 0,
                       passed=sum(r['when'] == 'call' and r['outcome'] == 'passed'
                                  and not r['wasxfail'] for r in outcome.get('reports', [])) if outcome else 0)
            packet['suites'].append(row)
            write(result, packet)
            print(f"{suite}: {'GREEN' if ok else 'RED'} selected={row['selected']} passed={row['passed']} exit={code}", flush=True)
    packet.update(owned_case_root_absent=not case.exists(),
                  suites_run=len(packet['suites']), suites_passed=sum(row['ok'] for row in packet['suites']),
                  tests_selected=sum(row['selected'] for row in packet['suites']),
                  tests_passed=sum(row['passed'] for row in packet['suites']))
    packet['gate_exit'] = 0 if (packet['owned_case_root_absent'] and
        len(packet['suites']) == len(checked['run']) and all(row['ok'] for row in packet['suites'])) else 1
    write(result, packet)
    print('NEXT_OFFLINE_GREEN' if packet['gate_exit'] == 0 else 'NEXT_OFFLINE_RED', flush=True)
    return packet['gate_exit']


if __name__ == '__main__':
    raise SystemExit(main())
