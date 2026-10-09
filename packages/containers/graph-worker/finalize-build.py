#!/usr/local/bin/python
"""Finalize graph build layers; never run during worker startup."""
import argparse
import compileall
from pathlib import Path
import py_compile

RUNTIME_BYPRODUCTS = (
    'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
    'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db',
    'tmp/.ses',
)
SYSTEM_BYPRODUCTS = (
    'var/log/apt/history.log', 'var/log/apt/term.log',
    'var/log/dpkg.log', 'var/cache/ldconfig/aux-cache',
)
BYPRODUCTS = RUNTIME_BYPRODUCTS + SYSTEM_BYPRODUCTS
COMPILE_ROOTS = ('opt/bcrg', 'usr/local/lib/python3.13', 'app',
                 'usr/local/libexec/kaidera')


def finalize(root, *, compile_roots=COMPILE_ROOTS, builder_home=False,
             cleanup_scope='all'):
    root = Path(root).resolve()
    paths = {'all': BYPRODUCTS, 'runtime': RUNTIME_BYPRODUCTS,
             'system': SYSTEM_BYPRODUCTS, 'none': ()}[cleanup_scope] + (
        ('root/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
         'root/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db')
        if builder_home and cleanup_scope in ('all', 'runtime') else ()
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


def verify_clean(root):
    """Final compilation refuses late cleanup of an earlier creating layer."""
    for name in BYPRODUCTS:
        try:
            (Path(root) / name).lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError(f'byproduct remained after creating RUN: {name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup-only', action='store_true')
    parser.add_argument('--builder-home', action='store_true')
    parser.add_argument('--cleanup-scope', choices=('all', 'runtime', 'system'),
                        default='all')
    args = parser.parse_args()
    if not args.cleanup_only:
        verify_clean('/')
    finalize('/', compile_roots=() if args.cleanup_only else COMPILE_ROOTS,
             builder_home=args.builder_home,
             cleanup_scope=args.cleanup_scope if args.cleanup_only else 'none')
