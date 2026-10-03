"""Immutable image release identity; no environment-derived product labels.

Legacy/source images without the baked file remain explicitly unqualified.
Signed-release policy and the supported sequence floor belong to the consumer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

_FIELDS = {"release_id", "release_lineage", "release_sequence", "api_contract", "source_revision"}
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


def validate_release_identity(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise RuntimeError("Invalid baked Cortex release identity")
    if any(not isinstance(value[key], str) or not _LABEL.fullmatch(value[key])
           for key in ("release_id", "release_lineage", "api_contract")):
        raise RuntimeError("Invalid baked Cortex release label")
    if type(value["release_sequence"]) is not int or not 1 <= value["release_sequence"] <= 2147483647:
        raise RuntimeError("Invalid baked Cortex release sequence")
    if not isinstance(value["source_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]):
        raise RuntimeError("Invalid baked Cortex source revision")
    return dict(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeError("Duplicate baked Cortex release identity field")
        result[key] = value
    return result


def load_release_identity() -> dict:
    path = Path(__file__).resolve().with_name("release_identity.json")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}  # Missing identity cannot pass CM-1's prerequisite gate.
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 4096:
            raise RuntimeError("Unsafe baked Cortex release identity")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("Oversized baked Cortex release identity")
        return validate_release_identity(json.loads(raw, object_pairs_hook=_pairs))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Unreadable baked Cortex release identity") from exc
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    # Build-time only: inputs are public release metadata, never runtime env.
    import sys
    if len(sys.argv) != 6:
        raise SystemExit("Supply release id, lineage, sequence, contract and source revision")
    release_id, lineage, sequence, contract, revision = sys.argv[1:]
    value = validate_release_identity({"release_id": release_id, "release_lineage": lineage,
        "release_sequence": int(sequence), "api_contract": contract, "source_revision": revision})
    destination = Path(__file__).resolve().with_name("release_identity.json")
    with destination.open("x", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    destination.chmod(0o444)
