"""The single canonical wire encoding for build inputs and receipts."""
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend


def _byte_length(validator, maximum, value, schema):
    if isinstance(value, str) and len(value.encode('utf-8')) > maximum:
        yield ValidationError('UTF-8 byte bound exceeded')


def _unique_items(validator, unique, value, schema):
    # Distinct string paths prove distinct wire objects without an O(n²) walk.
    # Duplicate/missing/non-string paths retain the upstream general semantics.
    if unique and validator.is_type(value,'array') and all(
            type(row) is dict and type(row.get('path')) is str for row in value):
        paths=[row['path'] for row in value]
        if len(set(paths))==len(paths):return
    yield from Draft202012Validator.VALIDATORS['uniqueItems'](validator,unique,value,schema)


WireValidator = extend(Draft202012Validator, {'maxLength': _byte_length,'uniqueItems':_unique_items})


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


# Same Unicode ranges as the original per-character rule, searched in C.
_FORBIDDEN=re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]')
_FORBIDDEN_PAYLOAD=re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]')


def _strings(value, *, text_payload=False):
    if isinstance(value, str):
        if not text_payload and unicodedata.normalize('NFC', value) != value:
            raise ValueError('unicode_not_nfc')
        if (_FORBIDDEN_PAYLOAD if text_payload else _FORBIDDEN).search(value):
            raise ValueError('forbidden_unicode')
    elif isinstance(value, dict):
        for key, item in value.items():
            _strings(key, text_payload=text_payload)
            _strings(item, text_payload=text_payload)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, text_payload=text_payload)
    elif text_payload and type(value) is float and math.isfinite(value):
        pass
    elif value is not None and type(value) not in (int, bool):
        raise ValueError('wrong_json_type')


def canonical_bytes(value):
    _strings(value)
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                       allow_nan=False) + '\n').encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _number(_):
    raise ValueError('noninteger_number')


def decode_json(raw, limit, *, canonical=True, text_payload=False):
    if type(raw) is not bytes or len(raw) > limit or raw.startswith(b'\xef\xbb\xbf'):
        raise ValueError('encoding_or_size')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_float=float if text_payload else _number, parse_constant=_number)
        # canonical_bytes already enforces the same strict string/type rules.
        if not canonical or text_payload:_strings(value, text_payload=text_payload)
        if canonical and canonical_bytes(value) != raw:
            raise ValueError('noncanonical_encoding')
        return value
    except (UnicodeError, RecursionError, OverflowError, json.JSONDecodeError) as error:
        raise ValueError('invalid_json') from error


def schema_validate(value, name):
    path = Path(__file__).resolve().parents[3] / 'contracts' / 'release' / name
    schema = json.loads(path.read_bytes())
    errors = WireValidator(schema).iter_errors(value)
    error = next(errors, None)
    if error:
        raise ValueError('schema_violation:' + '/'.join(map(str, error.absolute_path)))


def relative_path(path):
    if not isinstance(path, str) or not path or len(path.encode('utf-8')) > 4096:
        raise ValueError('invalid_path')
    if path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('invalid_path')
    return path


def ordered_inventory(rows):
    paths = [relative_path(row['path']) for row in rows]
    if paths != sorted(set(paths)):
        raise ValueError('inventory_not_sorted_unique')
    return rows
