"""The single canonical wire encoding for build inputs and receipts."""
import hashlib
import json
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend


def _byte_length(validator, maximum, value, schema):
    if isinstance(value, str) and len(value.encode('utf-8')) > maximum:
        yield ValidationError('UTF-8 byte bound exceeded')


WireValidator = extend(Draft202012Validator, {'maxLength': _byte_length})


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _strings(value):
    if isinstance(value, str):
        if unicodedata.normalize('NFC', value) != value:
            raise ValueError('unicode_not_nfc')
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('forbidden_unicode')
    elif isinstance(value, dict):
        for key, item in value.items():
            _strings(key)
            _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item)
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


def decode_json(raw, limit, *, canonical=True):
    if type(raw) is not bytes or len(raw) > limit or raw.startswith(b'\xef\xbb\xbf'):
        raise ValueError('encoding_or_size')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_float=_number, parse_constant=_number)
        _strings(value)
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
