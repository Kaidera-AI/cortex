"""API image inputs (J01): the two hash locks must cover what the API composition can import (asyncpg, and the signature-verification closure
cryptography -> cffi -> pycparser), agree across arches, and agree with the committed inputs receipt of the ONE archive. Pure and offline: no skips, no
environment. Byte checks of the archive and the image smoke are the scripts in ../tools (verify_inputs.py, smoke_image.py), run by the build."""
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
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


RECEIPT = HERE.parent / 'inputs-receipt.json'


def receipt():
    assert RECEIPT.is_file(), 'SURFACE_ABSENCE:inputs-receipt.json'
    return json.loads(RECEIPT.read_text())


def norm(name):
    return name.lower().replace('_', '-')


class LockCoverage(unittest.TestCase):
    def test_lock_covers_runtime_imports(self):
        need = runtime_imports()
        self.assertTrue({'asyncpg', 'cryptography'} <= need, 'the closure scan must see the async driver and the signature provider')
        for arch in ARCHES:
            self.assertEqual(sorted(need - set(read_lock(arch))), [], f'imports with no locked wheel on {arch}')

    def test_lock_is_closed_under_the_wheels_own_requirements(self):
        for arch in ARCHES:
            lock = read_lock(arch)
            for row in receipt()['wheels'][arch]:
                for dep in row['requires']:
                    self.assertIn(norm(dep), lock, f'{row["name"]} needs {dep} on {arch}')

    def test_both_arch_locks_pin_the_same_distributions_and_versions(self):
        a, b = read_lock('amd64'), read_lock('arm64')
        self.assertEqual({k: v[0] for k, v in a.items()}, {k: v[0] for k, v in b.items()})

    def test_reviewed_pins(self):
        for arch in ARCHES:
            lock = read_lock(arch)
            self.assertEqual(lock['asyncpg'][0], '0.32.0')
            self.assertEqual(lock['cryptography'][0], '50.0.0')  # release/ed25519.py refuses any other version


class LockMatchesReceipt(unittest.TestCase):
    def test_lock_hashes_equal_the_receipt_hashes_per_arch(self):
        for arch in ARCHES:
            lock = read_lock(arch)
            rows = {norm(r['name']): r for r in receipt()['wheels'][arch]}
            self.assertEqual(sorted(lock), sorted(rows), f'distributions differ on {arch}')
            for name, (version, digest) in lock.items():
                self.assertEqual((rows[name]['version'], rows[name]['sha256']), (version, digest), f'{name} on {arch}')

    def test_every_wheel_records_licence_source_and_size(self):
        for arch in ARCHES:
            for r in receipt()['wheels'][arch]:
                self.assertTrue(r['licence'] and r['licence'] != 'UNKNOWN', r['name'])
                self.assertTrue(r['source'], r['name'])
                self.assertGreater(r['bytes'], 0, r['name'])

    def test_licence_obligations_are_written_down_per_distribution(self):
        text = (HERE.parent / 'UPSTREAM.md').read_text().lower()
        for name, (version, _) in read_lock('arm64').items():
            self.assertIn(f'| {name} {version}', text, f'UPSTREAM.md needs a table row for {name} {version}')


class ClosureWiring(unittest.TestCase):
    """J06: the sole catalog validator ships INSIDE the API image closure as cortex_ship.install.roles (same module bytes, same contract), on both arches."""
    ROLES = 'COPY next/release/ship/install/roles.py /opt/cortex/next/src/cortex_ship/install/roles.py'
    PROVISION = 'COPY next/release/ship/install/provision.py /opt/cortex/next/src/cortex_ship/install/provision.py'
    CONTRACT = 'COPY next/contracts/runtime/runtime-operations-v1.json /opt/cortex/next/src/cortex_ship/install/runtime-operations-v1.json'

    def lines(self):
        return (HERE.parent / 'Containerfile').read_text().splitlines()

    def test_the_containerfile_copies_the_one_module_and_its_contract_into_the_fixed_namespace_before_bytecode_compilation(self):
        lines = self.lines()
        self.assertIn(self.ROLES, lines)
        self.assertIn(self.PROVISION, lines)
        self.assertIn(self.CONTRACT, lines)
        src = lines.index('COPY next/src/ /opt/cortex/next/src/')
        compile_at = next(i for i, l in enumerate(lines) if 'compileall' in l)
        self.assertLess(src, lines.index(self.ROLES))
        self.assertLess(src, lines.index(self.PROVISION))
        self.assertLess(lines.index(self.PROVISION), compile_at)
        self.assertLess(lines.index(self.CONTRACT), compile_at)

    def test_the_copied_sources_exist_in_the_build_context_and_nothing_else_is_staged_into_cortex_ship(self):
        self.assertTrue((NEXT / 'release/ship/install/roles.py').is_file())
        self.assertTrue((NEXT / 'release/ship/install/provision.py').is_file())
        self.assertTrue((NEXT / 'contracts/runtime/runtime-operations-v1.json').is_file())
        extra = [l for l in self.lines() if 'cortex_ship' in l and l not in (self.ROLES, self.PROVISION, self.CONTRACT)]
        self.assertEqual(extra, [])

    def test_a_tree_laid_out_exactly_like_the_copy_lines_imports_without_package_markers(self):
        root = Path(tempfile.mkdtemp(prefix='nix-wiring-'))
        self.addCleanup(shutil.rmtree, root, True)
        dest = root / 'cortex_ship/install'
        dest.mkdir(parents=True)
        shutil.copy(NEXT / 'release/ship/install/roles.py', dest / 'roles.py')
        shutil.copy(NEXT / 'release/ship/install/provision.py', dest / 'provision.py')
        shutil.copy(NEXT / 'contracts/runtime/runtime-operations-v1.json', dest / 'runtime-operations-v1.json')
        code = "import sys; sys.path.insert(0, sys.argv[1])\nfrom cortex_ship.install.roles import validate_catalog, load_roles; from cortex_ship.install import provision; print(len(load_roles()))\n"
        r = subprocess.run([sys.executable, '-I', '-c', code, str(root)], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '5'), r.stderr)


if __name__ == '__main__':
    unittest.main()
