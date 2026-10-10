"""Copied native Python3.13 regression; never execute product checks on the host."""
import hashlib
import os
from pathlib import Path
import py_compile
import shlex
import struct
import subprocess
import unittest

HERE = Path(__file__).resolve().parents[1]
VENV = Path('/opt/bcrg')


class BuilderBytecodeTests(unittest.TestCase):
    def test_builder_commits_only_checked_hash_venv_bytecode(self):
        source = (HERE / 'Dockerfile').read_text().replace('\\\n', '')
        builder = source.split('\nFROM ', 1)[0]
        run = next(line[4:] for line in builder.splitlines()
                   if line.startswith('RUN ') and '/build/fetch-qwen3-model.py' in line)
        compilers = [part.strip() for part in run.split('&&') if '-m compileall' in part]
        hashes = []
        for timestamp in (1234567890, 1700000000):
            for cache in VENV.rglob('*.pyc'):
                cache.unlink()
            for name in ('lib/python3.13/site-packages/probe.py', 'bin/jp.py'):
                path = VENV / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('VALUE = frozenset({"red", "green", "blue"})\n')
                os.utime(path, (timestamp, timestamp))
                py_compile.compile(str(path), invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            before = list(VENV.rglob('*.pyc'))
            self.assertEqual(len(before), 2)
            self.assertTrue(all(struct.unpack('<I', p.read_bytes()[4:8])[0] == 0 for p in before))
            for compiler in compilers:
                args = shlex.split(compiler)
                environment = dict(os.environ)
                while args and '=' in args[0]:
                    key, value = args.pop(0).split('=', 1)
                    environment[key] = value
                result = subprocess.run(args, env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            caches = sorted(VENV.rglob('*.pyc'))
            self.assertEqual(len(caches), 2)
            flags = {str(p.relative_to(VENV)): struct.unpack('<I', p.read_bytes()[4:8])[0] for p in caches}
            self.assertEqual(set(flags.values()), {3}, 'builder still commits timestamp-mode pyc: '+str(flags))
            hashes.append({str(p.relative_to(VENV)): hashlib.sha256(p.read_bytes()).hexdigest() for p in caches})
        self.assertEqual(hashes[0], hashes[1], 'source mtimes changed committed venv bytes')


if __name__ == '__main__':
    unittest.main()
