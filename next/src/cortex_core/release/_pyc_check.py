"""The one checked-hash .pyc equivalence check (R3), stdlib only.

The same file runs in-process for fixture scope and, byte-identical, as
/opt/cortex-inspector/inspect.py inside the accepted inspector image under
`python -I -S -B`, where the interpreter matches the admitted toolchain. It
never executes a code object. Header/name/source-hash/source-origin checks
stay in the controller, before any candidate bytes reach this check.
"""
import base64
import hashlib
import importlib.util
import io
import json
import marshal
import os
import struct
import sys
from types import CodeType

REQUEST_PATH = '/cortex-request.json'
REQUEST_SCHEMA = 'cortex.inspector-request.v1'
RESPONSE_SCHEMA = 'cortex.inspector-response.v1'
MAGIC = {'3.12': 'cb0d0d0a', '3.13': 'f30d0d0a', '3.14': '2b0e0d0a'}
REQUEST_KEYS = {'schema', 'policy_sha256', 'closure_sha256', 'abi', 'optimization', 'rows'}
ROW_KEYS = {'cache', 'source', 'final_filename', 'cache_b64', 'source_b64'}


class Refused(ValueError):
    """A stable reason from this check; other ValueErrors are bytecode shape."""


def _compiled_fields_equal(actual, expected):
    """Compare compiler output including fields omitted by CodeType equality."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, CodeType):
        fields = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount',
                  'co_nlocals', 'co_stacksize', 'co_flags', 'co_code',
                  'co_consts', 'co_names', 'co_varnames', 'co_filename',
                  'co_name', 'co_qualname', 'co_firstlineno', 'co_linetable',
                  'co_exceptiontable', 'co_freevars', 'co_cellvars')
        return all(_compiled_fields_equal(getattr(actual, field), getattr(expected, field)) for field in fields)
    if isinstance(expected, tuple):
        return len(actual) == len(expected) and all(_compiled_fields_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, float):
        return struct.pack('!d', actual) == struct.pack('!d', expected)
    if isinstance(expected, complex):
        return struct.pack('!dd', actual.real, actual.imag) == struct.pack('!dd', expected.real, expected.imag)
    if isinstance(expected, frozenset):
        if len(actual) != len(expected):
            return False
        remaining = list(expected)
        for item in actual:
            for index, candidate in enumerate(remaining):
                if _compiled_fields_equal(item, candidate):
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining
    return actual == expected


def check(raw, source, filename, optimization):
    """Refuse unless raw's body is exactly compile(source, filename) here."""
    if (type(raw) is not bytes or type(source) is not bytes or len(raw) < 16
            or raw[:4] != importlib.util.MAGIC_NUMBER):
        raise Refused('python_verifier_compiler_unqualified')
    try:
        stream = io.BytesIO(raw[16:])
        code = marshal.load(stream)
        if stream.read(1) or not isinstance(code, CodeType):
            raise Refused('python_bytecode_shape')
        pending = [code]
        while pending:
            current = pending.pop()
            if current.co_filename != filename:
                raise Refused('python_final_filename_policy')
            pending.extend(c for c in current.co_consts if isinstance(c, CodeType))
        expected = compile(source, filename, 'exec', dont_inherit=True, optimize=optimization)
    except Refused:
        raise
    except (EOFError, ValueError, TypeError, SyntaxError, RecursionError) as error:
        raise Refused('python_bytecode_shape') from error
    if not _compiled_fields_equal(code, expected):
        raise Refused('python_compilation_policy')


def respond(request):
    """One closed reply for one request; any malformed request raises."""
    value = json.loads(request.decode('utf-8'))
    if type(value) is not dict or set(value) != REQUEST_KEYS or value['schema'] != REQUEST_SCHEMA:
        raise ValueError('inspector_request_shape')
    if type(value['optimization']) is not int or type(value['rows']) is not list:
        raise ValueError('inspector_request_shape')
    verdicts = []
    for row in value['rows']:
        if type(row) is not dict or set(row) != ROW_KEYS:
            raise ValueError('inspector_request_shape')
        try:
            check(base64.b64decode(row['cache_b64'], validate=True), base64.b64decode(row['source_b64'], validate=True),
                  row['final_filename'], value['optimization'])
            equal = True
        except ValueError:
            equal = False
        verdicts.append({'cache': row['cache'], 'equal': equal})
    reply = {'schema': RESPONSE_SCHEMA, 'request_sha256': hashlib.sha256(request).hexdigest(),
             'abi': {'version': '%d.%d.%d' % sys.version_info[:3], 'magic': importlib.util.MAGIC_NUMBER.hex(),
                     'machine': os.uname().machine},
             'verdicts': verdicts}
    return (json.dumps(reply, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def main():
    with open(REQUEST_PATH, 'rb') as request:
        raw = request.read()
    sys.stdout.buffer.write(respond(raw))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
