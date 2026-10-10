"""Bounded source preparation protects Git/ignored data and refuses unsafe roots."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('epoch', Path(__file__).with_name('build-manual-linux.py'))
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class PreparationTests(unittest.TestCase):
    def test_git_and_ignored_input_are_untouched(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)/'source'
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            tracked = root/'tracked'; tracked.write_bytes(b'exact source')
            subprocess.run(['git', '-C', str(root), 'add', 'tracked'], check=True)
            ignored = root/'ignored'; ignored.write_bytes(b'fixture only')
            (root/'.git/info/exclude').write_text('ignored\n')
            protected = [ignored, root/'.git', root/'.git/config', root/'.git/info/exclude']
            before = {str(p): p.stat().st_mtime_ns for p in protected}
            original = (tracked.read_bytes(), tracked.stat().st_mode, tracked.stat().st_uid, tracked.stat().st_gid)
            receipt = producer.normalize_checkout_mtimes(root)
            self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in protected})
            self.assertEqual(original, (tracked.read_bytes(), tracked.stat().st_mode, tracked.stat().st_uid, tracked.stat().st_gid))
            self.assertEqual(tracked.stat().st_mtime_ns, 1791586380*10**9)
            self.assertEqual({row['path'] for row in receipt['records']}, {'tracked', '.'})

    def test_symlink_source_root_refuses_without_changing_target(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)/'source'; root.mkdir()
            link = Path(scratch)/'link'; link.symlink_to(root, target_is_directory=True)
            before = root.stat().st_mtime_ns
            with self.assertRaisesRegex(ValueError, 'physical source'):
                producer.normalize_checkout_mtimes(link)
            self.assertEqual(root.stat().st_mtime_ns, before)


if __name__ == '__main__':
    unittest.main()
