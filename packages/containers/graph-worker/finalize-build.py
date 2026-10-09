#!/usr/local/bin/python
"""Finalize graph build layers; never run during worker startup."""
import argparse
import compileall
from pathlib import Path
import py_compile

BYPRODUCTS = (
    'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
    'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db',
    'tmp/.ses', 'var/log/apt/history.log', 'var/log/apt/term.log',
    'var/log/dpkg.log', 'var/cache/ldconfig/aux-cache',
)
COMPILE_ROOTS = ('opt/bcrg', 'usr/local/lib/python3.13', 'app',
                 'usr/local/libexec/kaidera')


def finalize(root, *, compile_roots=COMPILE_ROOTS, builder_home=False):
    root = Path(root).resolve()
    paths = BYPRODUCTS + (
        ('root/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
         'root/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db')
        if builder_home else ()
    )
    for name in paths:
        path = root / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            raise RuntimeError(f'expected build byproduct file: {name}')
    for name in compile_roots:
        directory = root / name
        if not directory.exists():
            continue
        # Pip may have compiled with a random --target staging filename. Never
        # retain that code object or timestamp header, including bin/jp.py.
        for path in directory.rglob('*.pyc'):
            path.unlink()
        if not compileall.compile_dir(
            str(directory), force=True, quiet=1, optimize=0,
            stripdir=str(root), prependdir='/',
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        ):
            raise RuntimeError(f'bytecode compilation failed: {name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup-only', action='store_true')
    parser.add_argument('--builder-home', action='store_true')
    args = parser.parse_args()
    finalize('/', compile_roots=() if args.cleanup_only else COMPILE_ROOTS,
             builder_home=args.builder_home)
