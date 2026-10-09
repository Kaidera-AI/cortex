"""Versioned C01 validation seam, independent of storage and optional modules."""
import json
from datetime import datetime
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _rfc3339(value):
    # jsonschema's optional date-time extra must not silently decide validation.
    if not isinstance(value, str):
        return True  # The declared JSON type handles non-strings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


class ContractError(ValueError):
    """An unsupported or malformed contract; never a successful empty result."""


def _validate(schema, value):
    try:
        Draft202012Validator(schema, format_checker=FORMAT_CHECKER).validate(value)
    except ValidationError as error:
        # Avoid echoing an untrusted value into status/log output.
        path = "/".join(str(part) for part in error.path)
        raise ContractError(f"Invalid contract at {path or '/'} ({error.validator})") from None
    return value


def validate_event(value):
    """Validate immutable envelope v1. Scope authorization is a Core concern."""
    schema = json.loads((CONTRACTS / "outbox-event.schema.json").read_text())
    return _validate(schema, value)


def validate_wire(name, value):
    """Validate C01's proposed wire types; not a legacy compatibility assertion."""
    document = json.loads((CONTRACTS / "openapi.json").read_text())
    schemas = document["components"]["schemas"]
    if not name.startswith("Proposed") or name not in schemas:
        raise ContractError("Unsupported wire schema")
    return _validate(schemas[name], value)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def validate_openapi(document):
    """Verify ownership, schema structure and references in the 128-route draft."""
    if document.get("openapi") != "3.1.0":
        raise ContractError("Unsupported OpenAPI version")
    ids = []
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            ids.append(operation["operationId"])
            if operation.get("x-c01-disposition") not in {"keep", "change", "retire"}:
                raise ContractError(f"Unowned route {method} {path}")
            if "x-c02-freeze-gaps" not in operation:
                raise ContractError(f"Missing C02 freeze boundary for {method} {path}")
    if len(ids) != 128 or len(set(ids)) != len(ids):
        raise ContractError("Inventory count or operation IDs differ")
    for node in _walk(document):
        if "$ref" not in node:
            continue
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise ContractError("External schema reference is not pinned")
        target = document
        try:
            for token in ref[2:].split("/"):
                target = target[token.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise ContractError(f"Unresolved reference {ref}") from None
    for schema in document["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    return len(ids)
