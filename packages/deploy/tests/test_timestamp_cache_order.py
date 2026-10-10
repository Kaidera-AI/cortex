"""Real compiler/stat build-layer control; pip and Buildah commit are declared adapters."""
import compileall
import os
from pathlib import Path
import py_compile
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
EPOCH = 1791586380
RECIPES = ['packages/api/Dockerfile', 'packages/containers/embed-worker/Dockerfile',
           'packages/containers/pdf-worker/Dockerfile']


def instructions(text):
    lines, pending = [], ''
    for line in text.splitlines():
        if not pending and (not line.strip() or line.lstrip().startswith('#')):
            continue
        pending += line.rstrip().removesuffix('\\')+' '
        if line.rstrip().endswith('\\'):
            continue
        lines.append(pending.strip())
        pending = ''
    return lines


def compiler_command(instruction):
    """Exact process-prefix parser; compiler/gold assertions below remain unchanged."""
    command = shlex.split(instruction.removeprefix('RUN '))
    environment = dict(os.environ)
    if command and '=' in command[0]:
        setting = command.pop(0)
        assert setting == 'SETUPTOOLS_USE_DISTUTILS=stdlib', 'unexpected compiler prefix'
        environment['SETUPTOOLS_USE_DISTUTILS'] = 'stdlib'
    return command, environment


def exercise(text, root):
    """Pip adapter creates real caches; each RUN commit stamps real physical input mtimes."""
    root.mkdir()
    cache_counts = []
    for instruction in instructions(text):
        if instruction.startswith('FROM '):
            if list(root.rglob('*.pyc')):
                cache_counts.append(check(root))
            for p in sorted(root.rglob('*'), reverse=True):
                if p.is_file(): p.unlink()
                elif p.is_dir(): p.rmdir()
        if not instruction.startswith('RUN '):
            continue
        if 'pip install' in instruction:
            for name in ['alpha', 'beta', 'gamma']:
                source = root/(name+'.py')
                source.write_text('answer = 7\n')
                os.utime(source, (EPOCH+12345, EPOCH+12345))
                py_compile.compile(str(source), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        if 'compileall.compile_dir' in instruction:
            # Execute the actual recipe compiler expression, redirect only purelib to owned fixture.
            command, environment = compiler_command(instruction)
            assert command[:2] == ['python', '-c'] and len(command) == 3
            body = 'import sysconfig; sysconfig.get_path=lambda name: '+repr(str(root))+'; '+command[2]
            subprocess.run([sys.executable, '-c', body], check=True, env=environment)
        # Published --timestamp commit semantics; never edit any cache byte.
        for path in root.rglob('*'):
            os.utime(path, (EPOCH, EPOCH))
    cache_counts.append(check(root))
    return cache_counts


def check(root):
    caches = list(root.rglob('*.pyc'))
    assert caches, 'nonempty EVERY-cache coverage required'
    for cache in caches:
        source = Path(__import__('importlib.util').util.source_from_cache(str(cache)))
        raw = cache.read_bytes()
        _, flags, mtime, size = struct.unpack('<4sIII', raw[:16])
        assert flags == 0, 'timestamp flags changed'
        assert mtime == int(source.stat().st_mtime), 'cache/source actual mtime differs: '+cache.name
        assert size == source.stat().st_size, 'cache/source size differs'
    return len(caches)


class TimestampBuildOrder(unittest.TestCase):
    def test_every_cache_matches_stamped_source_after_actual_recipe_RUN_order(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe), tempfile.TemporaryDirectory() as tmp:
                counts = exercise((ROOT/recipe).read_text(), Path(tmp)/'site-packages')
                self.assertTrue(all(n == 3 for n in counts))


if __name__ == '__main__':
    unittest.main()
