"""Run in the bounded regression fixture as real UID10001, never on the host."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ownership_finalizer', HERE / 'finalize-build.py')
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)
RUNTIME = (
    '/home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
    '/home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db',
    '/tmp/.ses',
)


def cleanup_hooks():
    source = (HERE / 'Dockerfile').read_text().replace('\\\n', '')
    user = '0'
    hooks = []
    for line in source.splitlines():
        if line.startswith('FROM '):
            user = '0'
        elif line.startswith('USER '):
            user = line.split()[1].split(':')[0]
        elif line.startswith('RUN ') and 'finalize-build.py --cleanup-only' in line:
            command = next(part.strip() for part in line[4:].split('&&')
                           if 'finalize-build.py --cleanup-only' in part)
            hooks.append((user, shlex.split(command)))
    return hooks


class OwnershipCleanupTests(unittest.TestCase):
    def test_real_runtime_uid_cleans_without_visiting_system_parent(self):
        self.assertEqual(os.getuid(), 10001)
        self.assertEqual(os.getgid(), 10001)
        parent = Path('/var/cache/ldconfig')
        metadata = parent.stat()
        self.assertEqual(metadata.st_uid, 0)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
        attestation = json.loads(Path('/proof/system-cleanup.json').read_text())
        self.assertEqual(attestation['uid'], 0)
        self.assertTrue(attestation['all_system_byproducts_absent'])
        # Missing child does not avoid EACCES: the real directory is unsearchable.
        with self.assertRaises(PermissionError):
            (parent / 'aux-cache').lstat()
        for name in RUNTIME:
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'owned-probe-byproduct')
        neighbor = Path('/home/kaidera/keep.log')
        neighbor.write_bytes(b'keep')
        command = next(argv for user, argv in cleanup_hooks() if user == '10001')
        command = [str(HERE / 'finalize-build.py') if x.endswith('/finalize-build.py') else x
                   for x in command]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(not Path(name).exists() for name in RUNTIME))
        self.assertEqual(neighbor.read_bytes(), b'keep')
        self.assertEqual(Path('/opt/kaidera-qwen3/model.onnx').read_bytes(), b'fixture-weights')

    def test_creating_runs_select_their_own_paths_and_keep_hash_compile(self):
        hooks = cleanup_hooks()
        self.assertEqual(len(hooks), 3)
        for user, argv in hooks:
            scope = 'runtime' if user == '10001' or '--builder-home' in argv else 'system'
            self.assertIn('--cleanup-scope', argv)
            self.assertEqual(argv[argv.index('--cleanup-scope') + 1], scope)
        source = (HERE / 'Dockerfile').read_text()
        self.assertIn('USER 0\nRUN PYTHONHASHSEED=0 /usr/local/bin/python /usr/local/libexec/kaidera/finalize-build.py', source)
        self.assertIn('USER 10001:10001\n\nEXPOSE', source)

    def test_permission_errors_remain_visible_for_unscoped_cleanup(self):
        self.assertEqual(os.getuid(), 10001)
        with self.assertRaises(PermissionError):
            finalizer.finalize('/', compile_roots=())


if __name__ == '__main__':
    unittest.main()
