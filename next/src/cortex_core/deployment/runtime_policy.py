"""Deployment requirements only: no signature, credential or host admission.

The bundled, digest-bound schema is a source contract. Reading it is not a
runtime observation; no instance configuration or external reference is read.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_SHA256 = "efa7e63ff20dfbab85153bac102e96fc8fe7bf04153148fd1d8ca302f5088b36"
POLICY_LIMIT = 65536
PROFILES = frozenset(("podman-linux-rootless.v1", "podman-macos-machine-rootless.v1"))
PLATFORMS = frozenset(("linux/amd64", "linux/arm64"))
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class RuntimePolicyRefusal(ValueError):
    """Stable value-free code and JSON pointer; never an authority token."""

    def __init__(self, code, pointer=""):
        self.code = code
        self.pointer = pointer
        super().__init__(code + (":" + pointer if pointer else ""))


def _require(condition, code, pointer=""):
    if not condition:
        raise RuntimePolicyRefusal(code, pointer)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "runtime_policy_duplicate_key")
        result[key] = value
    return result


def _reject_number(value):
    raise RuntimePolicyRefusal("runtime_policy_noninteger_number")


def _decode_json(raw, limit):
    _require(type(raw) is bytes and 0 < len(raw) <= limit and not raw.startswith(b"\xef\xbb\xbf"),
             "runtime_policy_encoding_or_size")
    try:
        return json.loads(raw.decode("utf8"), object_pairs_hook=_pairs,
                          parse_float=_reject_number, parse_constant=_reject_number)
    except RuntimePolicyRefusal:
        raise
    except (UnicodeError, RecursionError, OverflowError, ValueError):
        raise RuntimePolicyRefusal("runtime_policy_invalid_json") from None


def _canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise RuntimePolicyRefusal("runtime_policy_invalid_json") from None


def _version(value):
    _require(type(value) is str and 0 < len(value.encode("utf8")) <= 128 and
             _VERSION.fullmatch(value) is not None, "runtime_policy_version")
    return tuple(int(part) for part in value.split("."))


def _canonical_date(value, pointer):
    try:
        parsed = date.fromisoformat(value)
        _require(parsed.isoformat() == value, "runtime_policy_date", pointer)
        return parsed
    except (TypeError, ValueError):
        raise RuntimePolicyRefusal("runtime_policy_date", pointer) from None


def _validate_denials(profile, pointer):
    denied = profile["denylist"]
    as_of = _canonical_date(denied["as_of"], pointer + "/denylist/as_of")
    _require(as_of >= date(2026, 10, 6), "runtime_policy_stale_denylist", pointer + "/denylist/as_of")
    versions = set()
    for index, entry in enumerate(denied["entries"]):
        where = pointer + "/denylist/entries/" + str(index)
        _version(entry["version"])
        _require(_canonical_date(entry["date"], where + "/date") <= as_of,
                 "runtime_policy_denial_date", where + "/date")
        _require(entry["version"] not in versions, "runtime_policy_duplicate_denial", where + "/version")
        versions.add(entry["version"])
        try:
            reason = entry["reason"]
            valid = (0 < len(reason.encode("utf8")) <= 1024 and
                     not any(ord(char) < 32 or ord(char) == 127 for char in reason))
        except UnicodeError:
            valid = False
        _require(valid, "runtime_policy_reason", where + "/reason")


# Local $refs only, fixed by the accepted U1 digest. This is not caller policy.
_SCHEMA_BYTES = (Path(__file__).resolve().parents[3] / "contracts/deployment/runtime-policy.schema.json").read_bytes()
_require(_sha256(_SCHEMA_BYTES) == SCHEMA_SHA256, "runtime_policy_source_schema")
_SCHEMA = json.loads(_SCHEMA_BYTES)
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


@dataclass(frozen=True)
class ParsedRuntimePolicy:
    document: Mapping
    canonical: bytes
    sha256: str
    raw_sha256: str


def parse_runtime_policy(raw: bytes) -> ParsedRuntimePolicy:
    """Parse deployment requirements. Successful parsing admits no runtime."""
    value = _decode_json(raw, POLICY_LIMIT)
    error = next(_VALIDATOR.iter_errors(value), None)
    if error is not None:
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
        raise RuntimePolicyRefusal("runtime_policy_shape", pointer)
    for profile_id, profile in value["profiles"].items():
        pointer = "/profiles/" + profile_id
        minimum = _version(profile["minimum_version"])
        _require(minimum >= (6, 0, 2), "runtime_policy_baseline", pointer + "/minimum_version")
        _validate_denials(profile, pointer)
    canonical = _canonical_bytes(value)
    _require(len(canonical) <= POLICY_LIMIT, "runtime_policy_encoding_or_size")
    return ParsedRuntimePolicy(_freeze(value), canonical, _sha256(canonical), _sha256(raw))


def _checked_policy(parsed):
    _require(type(parsed) is ParsedRuntimePolicy, "runtime_policy_record")
    checked = parse_runtime_policy(parsed.canonical)
    _require(checked.sha256 == parsed.sha256 and _canonical_bytes(_thaw(parsed.document)) == checked.canonical,
             "runtime_policy_record")
    return checked


def canonical_runtime_policy_bytes(parsed: ParsedRuntimePolicy) -> bytes:
    return _checked_policy(parsed).canonical


def runtime_policy_sha256(parsed: ParsedRuntimePolicy) -> str:
    return _checked_policy(parsed).sha256


def _platform_facts(values):
    _require(type(values) in (tuple, list) and 1 <= len(values) <= 2 and
             all(type(v) is str and v in PLATFORMS for v in values) and
             len(set(values)) == len(values), "runtime_policy_platform_facts")
    return set(values)


@dataclass(frozen=True)
class EffectiveRuntimePolicy:
    profile_id: str
    minimum_version: str
    platforms: tuple
    denied_versions: tuple
    denial_sources: tuple
    required_capabilities: tuple
    input_sha256: Mapping


def effective_runtime_policy(cortex_policy, kos_policy, *, profile_id, host_os, platform,
                             image_platforms, host_platforms) -> EffectiveRuntimePolicy:
    """Combine independently supplied constraints; no engine is observed here."""
    left, right = _checked_policy(cortex_policy), _checked_policy(kos_policy)
    _require(type(profile_id) is str and profile_id in PROFILES, "runtime_profile_unqualified")
    _require(profile_id in left.document["profiles"] and profile_id in right.document["profiles"],
             "runtime_policy_profile_intersection")
    profiles = (left.document["profiles"][profile_id], right.document["profiles"][profile_id])
    for key in ("engine", "host_os", "backend", "provider"):
        _require(profiles[0][key] == profiles[1][key], "runtime_policy_profile_conflict", "/" + key)
    _require(host_os == profiles[0]["host_os"], "runtime_policy_host")
    platforms = (set(profiles[0]["platforms"]) & set(profiles[1]["platforms"]) &
                 _platform_facts(image_platforms) & _platform_facts(host_platforms))
    _require(type(platform) is str and platform in platforms, "runtime_policy_platform_intersection")
    minimum = max((p["minimum_version"] for p in profiles), key=_version)
    denials = tuple(_freeze({"product": product, **_thaw(entry)})
                    for product, profile in zip(("cortex", "kos"), profiles)
                    for entry in profile["denylist"]["entries"])
    denied_versions = tuple(sorted({entry["version"] for entry in denials}, key=_version))
    capabilities = tuple(sorted(set(profiles[0]["required_capabilities"]) | set(profiles[1]["required_capabilities"])))
    return EffectiveRuntimePolicy(profile_id, minimum, tuple(sorted(platforms)), denied_versions,
                                  denials, capabilities, _freeze({"cortex": left.sha256, "kos": right.sha256}))
