"""API image inputs (J01): the two hash locks must cover what the serve entry can import, agree across arches, and match the ONE archive.
No network, no podman. Archive tests need CORTEX_BUILD_ARCHIVE (the frozen, hash-pinned input folder); the image smoke needs CORTEX_API_IMAGE."""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEXT = HERE.parents[3]
SRC = NEXT / 'src'
STD = set(sys.stdlib_module_names)
# The serve entry `python -m cortex_core.runtime api` is interface-only at this base (RC3a), so the API composition is named by the modules
# that build the connections and serve routes. Day 2 adds the real serve module here; the scan then covers it with no other change.
ENTRIES = ('cortex_core.runtime.__main__', 'cortex_core.runtime.connections', 'cortex_core.runtime.pg_adapters', 'cortex_core.api.c11b2')
ARCHES = ('amd64', 'arm64')
# Import names that are not their distribution name: none today; identity otherwise.
DIST = {}
# Names reachable from the API composition (ENTRIES) that are NOT locked and are open with the owner. A name here must stay missing from the
# locks (test_open_consults_are_named), so the day cox answers, the entry is deleted or the lock gains it - never both silently.
CONSULT_OPEN = {'cryptography': 'row f11f8ccc consult to cox: runtime lock verification imports it lazily (release/ed25519.py:28) and the API lock has no wheel'}
LINE = re.compile(r'^([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+!-]*) --hash=sha256:([0-9a-f]{64})$')


def read_lock(arch):
    out = {}
    for raw in (HERE.parent / f'requirements.linux-{arch}.lock').read_text().splitlines():
        if not raw.strip() or raw.startswith('#'):
            continue
        m = LINE.match(raw)
        assert m, f'lock line not name==version --hash=sha256:<64 hex>: {raw!r}'
        out[m.group(1).lower().replace('_', '-')] = (m.group(2), m.group(3))
    return out


def modfile(name):
    p = SRC.joinpath(*name.split('.'))
    if p.with_suffix('.py').is_file():
        return p.with_suffix('.py')
    if (p / '__init__.py').is_file():
        return p / '__init__.py'
    return None


def runtime_imports():
    """Third-party top-level names reachable from the API composition (ENTRIES): every import statement (also inside functions) of every
    cortex_core module reached, plus the database drivers the connection policy names as `driver in (...)`."""
    seen, third = {}, set()

    def top(mod, base):
        head = mod.split('.')[0]
        if head == 'cortex_core':
            visit(mod)
        elif head not in STD:
            third.add(head)

    def visit(name):
        f = modfile(name)
        if f is None or name in seen:
            return
        seen[name] = f
        parts = name.split('.')
        for i in range(1, len(parts)):
            visit('.'.join(parts[:i]))
        text = f.read_text()
        for m in re.finditer(r'\bdriver in \(([^)]*)\)', text):
            third.update(re.findall(r"'([A-Za-z0-9_]+)'", m.group(1)))
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                for a in n.names:
                    top(a.name, name)
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    pkg = name if f.name == '__init__.py' else name.rsplit('.', 1)[0]
                    base = pkg.split('.')
                    mod = '.'.join(base[:len(base) - (n.level - 1)] + ([n.module] if n.module else []))
                else:
                    mod = n.module
                if mod:
                    top(mod, name)
                    for a in n.names:
                        if modfile(mod + '.' + a.name):
                            visit(mod + '.' + a.name)

    for entry in ENTRIES:
        visit(entry)
    return {DIST.get(t, t).lower().replace('_', '-') for t in third}


def archive():
    p = os.environ.get('CORTEX_BUILD_ARCHIVE')
    if not p or not Path(p, 'SHA256SUMS').is_file():
        raise unittest.SkipTest('CORTEX_BUILD_ARCHIVE not set to an archive folder')
    return Path(p)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class LockCoverage(unittest.TestCase):
    def test_lock_covers_runtime_imports(self):
        need = runtime_imports() - set(CONSULT_OPEN)
        self.assertIn('asyncpg', need, 'the closure scan must see the asyncpg driver the runtime calls')
        for arch in ARCHES:
            self.assertEqual(sorted(need - set(read_lock(arch))), [], f'imports with no locked wheel on {arch}')

    def test_open_consults_are_named(self):
        for arch in ARCHES:
            self.assertEqual(sorted(set(CONSULT_OPEN) & set(read_lock(arch))), [], 'a name in CONSULT_OPEN is locked now: delete the entry')
        self.assertLessEqual(set(CONSULT_OPEN), runtime_imports(), 'a CONSULT_OPEN name is no longer imported: delete the entry')

    def test_both_arch_locks_pin_the_same_distributions_and_versions(self):
        a, b = read_lock('amd64'), read_lock('arm64')
        self.assertEqual({k: v[0] for k, v in a.items()}, {k: v[0] for k, v in b.items()})

    def test_asyncpg_is_pinned_to_the_reviewed_release(self):
        for arch in ARCHES:
            self.assertEqual(read_lock(arch)['asyncpg'][0], '0.32.0')


class LockMatchesArchive(unittest.TestCase):
    def test_every_locked_wheel_is_in_the_archive_and_nothing_else_is(self):
        root = archive()
        for arch in ARCHES:
            wheels = root / f'linux-{arch}' / 'assets' / 'wheels'
            by_hash = {sha(p): p.name for p in wheels.iterdir() if p.suffix == '.whl'}
            lock = {h for _, h in read_lock(arch).values()}
            self.assertEqual(sorted(lock - set(by_hash)), [], f'locked hash with no wheel file on {arch}')
            self.assertEqual(sorted(set(by_hash) - lock), [], f'wheel file with no lock line on {arch}')

    def test_archive_verifies_against_its_sums(self):
        root = archive()
        bad = []
        for line in (root / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            if not (root / name).is_file() or sha(root / name) != digest:
                bad.append(name)
        self.assertEqual(bad, [])

    def test_receipt_lists_every_file_and_records_a_licence_for_asyncpg(self):
        root = archive()
        receipt = json.loads((root / 'RECEIPT.json').read_text())
        sums = {l.split('  ', 1)[1]: l.split('  ', 1)[0] for l in (root / 'SHA256SUMS').read_text().splitlines()}
        self.assertEqual({r['path']: r['sha256'] for r in receipt['files']}, sums)
        rows = [r for r in receipt['files'] if '/asyncpg-' in r['path']]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get('licence') == 'Apache-2.0' for r in rows))


class ImageSmoke(unittest.TestCase):
    def test_smoke_api_imports(self):
        image = os.environ.get('CORTEX_API_IMAGE')
        if not image:
            raise unittest.SkipTest('CORTEX_API_IMAGE not set')
        code = 'import jsonschema, psycopg, asyncpg, uvicorn, starlette, httpx, cortex_core.runtime; print(asyncpg.__version__)'
        r = subprocess.run(['podman', 'run', '--rm', '--network=none', '--entrypoint', '/opt/cortex/python/bin/python3', image, '-I', '-c', code],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '0.32.0'), r.stderr[-400:])


if __name__ == '__main__':
    unittest.main()
