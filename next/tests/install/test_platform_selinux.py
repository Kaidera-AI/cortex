"""Source contract for V's effective-policy observation (design3 sec. 2.3).

A temporary directory stands in for selinuxfs: DATA only, no host credit. The
real kernel read is tests/install/native/test_linux_native.py (Linux lab only).
"""
import ast
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from cortex_core.install import owner
from cortex_core.install.platforms import _selinux

SUBJECT = Path(__file__).resolve().parents[2] / 'src/cortex_core/install/platforms/_selinux.py'
POLICY = bytes.fromhex('8cff7cf9') + b'policydb' * 4096


def page(version=1, sequence=4, enforcing=1, policyload=1, deny_unknown=0):
    return struct.pack('<5I', version, sequence, enforcing, policyload, deny_unknown)


class EffectivePolicy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.write(page(), POLICY)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, status=None, policy=None):
        if status is not None:
            Path(self.root, 'status').write_bytes(status)
        if policy is not None:
            Path(self.root, 'policy').write_bytes(policy)

    def refuses(self, detail, fn, *args, **kw):
        with self.assertRaises(owner.OwnerRefusal) as got:
            fn(*args, root=self.root, **kw)
        self.assertEqual(got.exception.detail, detail)

    def test_receipt_member_is_observed_digest_and_both_status_reads(self):
        observed = _selinux.start(root=self.root)
        member = _selinux.verdict(observed, root=self.root)
        status = {'version': 1, 'sequence': 4, 'enforcing': 1, 'policyload': 1, 'deny_unknown': 0}
        self.assertEqual(member, {'policy_sha256': hashlib.sha256(POLICY).hexdigest(), 's0': status, 's1': status})

    def test_policy_load_boolean_or_setenforce_between_start_and_verdict_refuses(self):
        # lab L-P2/L-P4: a boolean commit bumps policyload, setenforce flips enforcing; both move the sequence
        for changed in (page(sequence=6, policyload=2), page(sequence=6, enforcing=0), page(sequence=6)):
            with self.subTest(changed=changed):
                self.write(page())
                observed = _selinux.start(root=self.root)
                self.write(changed)
                self.refuses('custody_changed', _selinux.verdict, observed)

    def test_status_change_during_the_policy_read_refuses(self):
        real = _selinux._policy_sha256

        def reload_while_reading(root):
            digest = real(root)
            self.write(page(sequence=6, policyload=2))  # a policy load lands mid-read
            return digest

        _selinux._policy_sha256 = reload_while_reading
        try:
            self.refuses('custody_changed', _selinux.start)
        finally:
            _selinux._policy_sha256 = real

    def test_torn_unknown_or_missing_status_refuses(self):
        for bad in (page(sequence=5), page(version=0), page(enforcing=2), page(deny_unknown=7), page()[:19]):
            with self.subTest(bad=bad):
                self.write(bad)
                self.refuses('owner_unavailable', _selinux.start)
        Path(self.root, 'status').unlink()
        self.refuses('owner_unavailable', _selinux.start)

    def test_unreadable_empty_or_oversized_policy_refuses(self):
        self.write(policy=b'')
        self.refuses('owner_unavailable', _selinux.start)
        Path(self.root, 'policy').unlink()  # also stands in for -EBUSY / EACCES: any OSError
        self.refuses('owner_unavailable', _selinux.start)
        with open(Path(self.root, 'policy'), 'wb') as f:
            f.truncate(_selinux._POLICY_MAX + 1)
        self.refuses('owner_unavailable', _selinux.start)

    def test_verdict_takes_only_its_own_start_record(self):
        observed = _selinux.start(root=self.root)
        for bad in (None, {}, dict(observed, extra=1), {'policy_sha256': observed['policy_sha256']}):
            with self.subTest(bad=bad):
                self.refuses('owner_unavailable', _selinux.verdict, bad)

    def test_observation_only_no_expected_values_no_module_hash_no_namespaces(self):
        source = SUBJECT.read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or '.')
        self.assertEqual(imported, {'hashlib', 'struct', '.'})
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse({'policy', 'expected', 'namespaces', 'semodule'} & names)  # never reads O's values
        self.assertEqual(_selinux.SELINUXFS, '/sys/fs/selinux')
        self.assertEqual(_selinux.__all__, ())


if __name__ == '__main__':
    unittest.main()
