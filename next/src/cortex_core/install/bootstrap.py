"""Family 1: the installer-side protected bootstrap producer (Linux packaging).

The INSTALLER measures the selected owner closure (interpreter, entry, source,
stdlib, loader, crypto, data originals) BEFORE the owner (V) runs, and records it
canonically. The record is DATA for R01's existing startup admission: the fixed B1
producer delivers it as _Inputs.release_startup through startup_context(). Nothing
in the closure is executed or imported here; self-measurement inside V would not
create pre-execution provenance. Measurement uses the consumer's own reader
(inspector._read_member) as the single rule set, so a row the owner would refuse
is never emitted. The root is fixed ('/'); no caller selects paths outside the
closure, a root, or the pins. Real values come from the real host (group C/D).
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys

from cortex_core.release.codec import canonical_bytes, decode_json
from cortex_core.release.member_admission import _TYPING_ALIASES
from cortex_core.runtime.signed_member import Refusal

SELECTION_SCHEMA = 'cortex.release-bootstrap-selection.v1'
RECORD_SCHEMA = 'cortex.release-bootstrap.v1'
# The selected owner closure (cox gA1 'Later inputs' + kai Q2: the jsonschema closure
# release/codec imports); keys are the exact METADATA Name values. Not a caller choice.
SELECTED = {'cpython': '3.12.15', 'cryptography': '50.0.0', 'cffi': '2.1.1', 'pycparser': '3.0',
            'jsonschema': '4.25.1', 'attrs': '25.4.0', 'referencing': '0.37.0', 'rpds-py': '0.30.0',
            'jsonschema-specifications': '2025.9.1', 'typing_extensions': '4.16.0'}
_ROLES = frozenset(('interpreter', 'source', 'stdlib', 'loader', 'crypto', 'data'))
_REQUIRED = frozenset(('interpreter', 'source', 'stdlib', 'loader', 'crypto'))
_COMMON = frozenset(('schema', 'bootstrap_id', 'pins', 'executable', 'entrypoint', 'python_version',
                     'import_roots', 'cache_prefix', 'intrinsics', 'module_aliases'))
_SELECTION = _COMMON | {'dist_metadata', 'closure'}
_RECORD = _COMMON | {'files'}
_PINS = frozenset(SELECTED) | {'cpython_build_sha256'}
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_MAX_FILES = 20000  # inspector.MAX_MEMBERS, the consumer's own bound
_MAX_FILE = 256 * 1024 * 1024
_LIMIT = 8 * 1024 * 1024
_ROOT = Path('/')


def _require(value, reason):
    if not value:
        raise Refusal(reason)


def _absolute(value):
    return (type(value) is str and value.startswith('/') and 1 < len(value) <= 4096 and '\0' not in value
            and '//' not in value and not value.endswith('/')
            and all(part not in ('.', '..') for part in value.split('/')[1:]))


def _common(doc, reason):
    pins = doc['pins']
    _require(type(pins) is dict and set(pins) == _PINS and all(pins[k] == v for k, v in SELECTED.items())
             and type(pins['cpython_build_sha256']) is str and _HEX.fullmatch(pins['cpython_build_sha256']), reason)
    _require(type(doc['bootstrap_id']) is str and _ID.fullmatch(doc['bootstrap_id']), reason)
    _require(doc['python_version'] == [int(n) for n in SELECTED['cpython'].split('.')], reason)
    _require(all(_absolute(doc[k]) for k in ('executable', 'entrypoint', 'cache_prefix')), reason)
    roots = doc['import_roots']
    _require(type(roots) is list and 0 < len(roots) <= 32 and all(_absolute(r) for r in roots)
             and len(set(roots)) == len(roots), reason)
    names = doc['intrinsics']
    _require(type(names) is list and 0 < len(names) <= 512 and all(type(n) is str and 0 < len(n) <= 256 for n in names)
             and names == sorted(set(names)), reason)
    aliases = doc['module_aliases']
    _require(type(aliases) is list and len(aliases) <= 512
             and all(type(a) is list and len(a) == 2 and type(a[0]) is str and 0 < len(a[0]) <= 256
                     and _absolute(a[1]) for a in aliases)
             and [a[0] for a in aliases] == sorted({a[0] for a in aliases}), reason)


def _table(rows, doc, reason):
    """The consumer's table invariants (member_admission._startup_identity)."""
    paths = [r[1] for r in rows]
    roles = {r[0]: True for r in rows}
    native = {r[1] for r in rows if r[0] in ('loader', 'crypto')}
    typing = {r[1] for r in rows if r[0] == 'stdlib' and r[1].endswith('/typing.py')}
    _require(0 < len(rows) <= _MAX_FILES and paths == sorted(set(paths)) and _REQUIRED <= set(roles)
             and [doc['executable'], 'interpreter'] in [[r[1], r[0]] for r in rows]
             and [doc['entrypoint'], 'source'] in [[r[1], r[0]] for r in rows]
             and all(a[1] in native or (a[0] in _TYPING_ALIASES and a[1] in typing) for a in doc['module_aliases']), reason)


def parse_selection(raw):
    reason = 'bootstrap_selection_invalid'
    try:
        doc = decode_json(raw, _LIMIT)
        _require(type(doc) is dict and set(doc) == _SELECTION and doc['schema'] == SELECTION_SCHEMA, reason)
        _common(doc, reason)
        closure = doc['closure']
        _require(type(closure) is list and all(type(r) is list and len(r) == 2 and r[0] in _ROLES
                                               and _absolute(r[1]) for r in closure), reason)
        _table(closure, doc, reason)
        meta = doc['dist_metadata']
        _require(type(meta) is dict and set(meta) == set(SELECTED) - {'cpython'}
                 and all(['data', meta[k]] in closure for k in meta), reason)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Refusal(reason) from None
    return doc


def _measure(role, path):
    """Hash one original, then let the consumer's reader accept or refuse it."""
    from cortex_core.release import inspector
    real = _ROOT / path[1:]
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            meta = os.fstat(fd)
            _require(stat.S_ISREG(meta.st_mode) and meta.st_size <= _MAX_FILE, 'bootstrap_original_refused')
            digest, total = hashlib.sha256(), 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                _require(total <= _MAX_FILE, 'bootstrap_original_refused')
                digest.update(chunk)
        finally:
            os.close(fd)
        row = [role, path, meta.st_size, digest.hexdigest(), stat.S_IMODE(meta.st_mode)]
        inspector._read_member(_ROOT, dict(role=role, path=path[1:], bytes=row[2], sha256=row[3], mode=row[4]))
        return row, real
    except Refusal:
        raise
    except Exception:
        raise Refusal('bootstrap_original_refused') from None


def _metadata_version(raw, name):
    """Exactly one Name and one Version header in the core metadata header block."""
    try:
        head = raw.decode('utf-8').split('\n\n', 1)[0].splitlines()
    except UnicodeError:
        return None
    names = [l[6:] for l in head if l.startswith('Name: ')]
    versions = [l[9:] for l in head if l.startswith('Version: ')]
    return versions[0] if names == [name] and len(versions) == 1 else None


def measure_bootstrap(selection_raw):
    """Installer entry: measure the selected closure and return canonical record bytes."""
    doc = parse_selection(selection_raw)
    files, bodies = [], {}
    for role, path in doc['closure']:
        row, real = _measure(role, path)
        files.append(row)
        if role == 'data':
            bodies[path] = real
    interpreter = next(r for r in files if r[1] == doc['executable'])
    _require(interpreter[3] == doc['pins']['cpython_build_sha256'], 'bootstrap_pin_mismatch')
    for name, path in doc['dist_metadata'].items():
        row = next(r for r in files if r[1] == path)  # parse_selection proved it is a closure row
        raw = bodies[path].read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == row[3], 'bootstrap_original_refused')
        _require(_metadata_version(raw, name) == SELECTED[name], 'bootstrap_pin_mismatch')
    record = {k: doc[k] for k in _COMMON}
    record.update(schema=RECORD_SCHEMA, files=files)
    return canonical_bytes(record)


def parse_record(raw):
    reason = 'bootstrap_record_invalid'
    try:
        doc = decode_json(raw, _LIMIT)
        _require(type(doc) is dict and set(doc) == _RECORD and doc['schema'] == RECORD_SCHEMA, reason)
        _common(doc, reason)
        files = doc['files']
        _require(type(files) is list and all(
            type(r) is list and len(r) == 5 and r[0] in _ROLES and _absolute(r[1])
            and type(r[2]) is int and 0 <= r[2] <= _MAX_FILE and type(r[3]) is str and _HEX.fullmatch(r[3])
            and type(r[4]) is int and 0 <= r[4] <= 0o777 for r in files), reason)
        _table(files, doc, reason)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Refusal(reason) from None
    return doc


def startup_context(record_raw, *, owner_binding, current, originals):
    """B1 delivery inside V: the record plus V's own live hooks, nothing else.

    path_hooks and alias objects are the identities V actually holds; R01's
    _admit_startup later requires the running interpreter to match them and every
    file row. owner_binding/current/originals come from the current owner (cox).
    """
    from cortex_core.release.member_admission import _StartupContext
    doc = parse_record(record_raw)
    modules = sys.modules
    _require(all(modules.get(name) is not None for name, _ in doc['module_aliases']), 'bootstrap_record_invalid')
    return _StartupContext(
        bootstrap_id=doc['bootstrap_id'], executable=doc['executable'], entrypoint=doc['entrypoint'],
        python_version=tuple(doc['python_version']), import_roots=tuple(doc['import_roots']),
        cache_prefix=doc['cache_prefix'], files=tuple(tuple(r) for r in doc['files']),
        intrinsics=tuple(doc['intrinsics']), path_hooks=tuple(sys.path_hooks), owner_binding=owner_binding,
        current=current, originals=originals,
        module_aliases=tuple((name, modules[name], path) for name, path in doc['module_aliases']))
