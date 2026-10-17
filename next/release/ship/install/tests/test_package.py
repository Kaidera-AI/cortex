"""The ONE validator module ships into the Core closure as cortex_ship.install.roles (J06 interface): same bytes, same policy contract next to it, stdlib only, no
second copy. package_closure.py stages it; this test imports it from the staged tree in a clean interpreter and checks the receipt."""
import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
NEXT = HERE.parents[3]
STDLIB_OK = {'json', 'dataclasses', 'pathlib', 'copy', 'sys', 'os'}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Package(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nix-closure-'))
        self.out = self.tmp / 'closure'
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_pkg = subprocess.run([sys.executable, '-I', str(INSTALL / 'package_closure.py'), '--out', str(self.out)], capture_output=True, text=True)
        self.assertEqual(self.run_pkg.returncode, 0, self.run_pkg.stderr)

    def test_the_staged_files_are_the_same_bytes_and_nothing_else(self):
        staged = sorted(str(p.relative_to(self.out)) for p in self.out.rglob('*') if p.is_file())
        self.assertEqual(staged, ['cortex_ship/__init__.py', 'cortex_ship/install/__init__.py', 'cortex_ship/install/roles.py',
                                  'cortex_ship/install/runtime-operations-v1.json', 'cortex_ship/receipt.json'])
        self.assertEqual(sha(self.out / 'cortex_ship/install/roles.py'), sha(INSTALL / 'roles.py'))
        self.assertEqual(sha(self.out / 'cortex_ship/install/runtime-operations-v1.json'), sha(NEXT / 'contracts/runtime/runtime-operations-v1.json'))

    def test_the_receipt_binds_every_staged_file_by_digest(self):
        receipt = json.loads((self.out / 'cortex_ship/receipt.json').read_text())
        self.assertEqual(receipt['namespace'], 'cortex_ship.install.roles')
        listed = {f['path']: f['sha256'] for f in receipt['files']}
        for rel, digest in listed.items():
            self.assertEqual(sha(self.out / rel), digest, rel)
        self.assertEqual(sorted(listed), ['cortex_ship/__init__.py', 'cortex_ship/install/__init__.py', 'cortex_ship/install/roles.py', 'cortex_ship/install/runtime-operations-v1.json'])
        self.assertEqual(receipt['source_roles_sha256'], sha(INSTALL / 'roles.py'))
        self.assertEqual(receipt['platform_independent'], True)

    def test_the_fixed_import_works_in_a_clean_interpreter_and_loads_the_packaged_policy(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1])\n"
                "from cortex_ship.install.roles import validate_catalog, load_roles, CONTRACT\n"
                "import json; print(json.dumps({'contract': str(CONTRACT), 'five': list(load_roles()), 'callable': callable(validate_catalog)}))\n")
        r = subprocess.run([sys.executable, '-I', '-c', code, str(self.out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertTrue(got['callable'])
        self.assertEqual(got['five'], ['bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment'])
        self.assertEqual(Path(got['contract']).resolve(), (self.out / 'cortex_ship/install/runtime-operations-v1.json').resolve())

    def test_the_module_needs_only_the_standard_library_so_both_targets_get_the_same_closure(self):
        tree = ast.parse((INSTALL / 'roles.py').read_text())
        imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, STDLIB_OK)

    def test_a_missing_packaged_contract_refuses_instead_of_falling_back_to_a_tree_path(self):
        decoy = self.out.parent / 'contracts/runtime'   # exactly where a tree-relative fallback would look (module parents[3])
        decoy.mkdir(parents=True)
        shutil.copy(NEXT / 'contracts/runtime/runtime-operations-v1.json', decoy)
        self.addCleanup(shutil.rmtree, self.out.parent / 'contracts', True)
        (self.out / 'cortex_ship/install/runtime-operations-v1.json').unlink()
        code = ("import sys; sys.path.insert(0, sys.argv[1])\n"
                "from cortex_ship.install import roles\n"
                "try:\n    roles.load_roles(); print('LOADED')\nexcept roles.RoleRefusal as e:\n    print(e.reason)\n")
        r = subprocess.run([sys.executable, '-I', '-c', code, str(self.out)], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), 'roles_contract_invalid', r.stderr)

class PinnedKey(unittest.TestCase):
    """The release public key pinned in install.sh equals the AUTHORITATIVE KOS key, compared by full bytes (not the 8-byte key id). The fixture is the public key
    file Cox supplied (KOS commit 26107b5b0ac5, scripts/release/installer/kos_installer/release-public-key.pub, blob 5535ec7bf8a7); public data only, no key use."""
    FIXTURE = HERE / 'fixtures/KOS-release-public-key.pub'
    AUTH_SHA256 = 'cc789827d21f6db4b3d74c39709b78d2ab6a636b28489d58d3b7ec442b66c8ef'

    def test_the_authoritative_public_key_file_is_carried_byte_exact(self):
        self.assertTrue(self.FIXTURE.is_file(), 'tests/fixtures/KOS-release-public-key.pub')
        self.assertEqual((sha(self.FIXTURE), self.FIXTURE.stat().st_size), (self.AUTH_SHA256, 113))

    def test_install_sh_pins_exactly_the_key_line_of_that_file_and_its_key_id(self):
        self.assertTrue(self.FIXTURE.is_file())
        comment, key = self.FIXTURE.read_text().splitlines()
        import re
        m = re.search(r"^PINNED_PUBKEY='([^']+)'.*7BBB4B7E0CE8852E", (INSTALL / 'install.sh').read_text(), re.M)
        self.assertIsNotNone(m, 'install.sh must pin the key and name key id 7BBB4B7E0CE8852E')
        self.assertEqual(m.group(1), key)
        self.assertEqual(comment, 'untrusted comment: minisign public key 7BBB4B7E0CE8852E')


if __name__ == '__main__':
    unittest.main()
