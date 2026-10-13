"""Assemble unsigned deployment metadata from independent consistency facts.

The caller owns measured source/image provenance and subsequent signing. This
module neither verifies a signature nor certifies a package or a host.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re

from .runtime_policy import (SCHEMA_SHA256, _canonical_bytes, _checked_policy,
                             _decode_json, _platform_facts, _require, _sha256, _thaw)


IDENTITY_FIELDS = ("release_id", "release_lineage", "release_sequence", "api_contract", "source_revision")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE = re.compile(r"[0-9a-f]{40}")
_RELEASE = re.compile(r"v[0-9][A-Za-z0-9.+-]{0,127}")


def _valid_identity(identity):
    if not isinstance(identity, Mapping) or set(identity) != set(IDENTITY_FIELDS):
        return False
    sequence = identity["release_sequence"]
    if type(sequence) is not int or sequence < 1:
        return False
    for key, maximum in (("release_id", 128), ("release_lineage", 64), ("api_contract", 128), ("source_revision", 40)):
        value = identity[key]
        if type(value) is not str:
            return False
        try:
            if not 0 < len(value.encode("utf8")) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
                return False
        except UnicodeError:
            return False
    return _RELEASE.fullmatch(identity["release_id"]) is not None and _SOURCE.fullmatch(identity["source_revision"]) is not None


def _check_metadata_bindings(value, expected_identity, expected_images, source_bytes, source_sha256,
                             schema_bytes, schema_sha256, policy_sha256, expected_policy_sha256):
    _require(_valid_identity(expected_identity), "runtime_metadata_expected_identity")
    identity = {name: value.get(name) for name in IDENTITY_FIELDS}
    _require(_valid_identity(identity) and all(type(identity[k]) is type(expected_identity[k]) and
                                             identity[k] == expected_identity[k] for k in IDENTITY_FIELDS),
             "runtime_metadata_identity")
    _require(type(source_bytes) is bytes and type(source_sha256) is str and
             _SHA256.fullmatch(source_sha256) is not None and _sha256(source_bytes) == source_sha256,
             "runtime_metadata_source_binding")
    _require(type(schema_bytes) is bytes and type(schema_sha256) is str and
             _SHA256.fullmatch(schema_sha256) is not None and
             _sha256(schema_bytes) == schema_sha256 == SCHEMA_SHA256, "runtime_metadata_schema_binding")
    _require(type(expected_policy_sha256) is str and _SHA256.fullmatch(expected_policy_sha256) is not None and
             policy_sha256 == expected_policy_sha256, "runtime_metadata_policy_binding")
    _require(isinstance(expected_images, Mapping) and bool(expected_images) and type(value.get("images")) is dict and
             _canonical_bytes(value["images"]) == _canonical_bytes(_thaw(expected_images)), "runtime_metadata_image_binding")


@dataclass(frozen=True)
class UnsignedRuntimeMetadataDraft:
    raw: bytes
    sha256: str
    policy_sha256: str
    source_sha256: str
    schema_sha256: str
    profile_id: str


def assemble_runtime_metadata(*, release_metadata: bytes, expected_identity, expected_images,
                              source_bytes: bytes, source_sha256: str, schema_bytes: bytes,
                              schema_sha256: str, runtime_policy, expected_policy_sha256: str,
                              profile_id: str, host_os: str, platform: str,
                              image_platforms, host_platforms) -> UnsignedRuntimeMetadataDraft:
    """Return draft bytes only; independent input comparisons grant no authority."""
    value = _decode_json(release_metadata, 1048576)
    _require(type(value) is dict, "runtime_metadata_object")
    _require(not ({"podman", "signature_ok", "native_qualified", "admitted", "qualified", "_seal"} & set(value)),
             "runtime_metadata_ambiguous_authority")
    parsed = _checked_policy(runtime_policy)
    _check_metadata_bindings(value, expected_identity, expected_images, source_bytes, source_sha256,
                             schema_bytes, schema_sha256, parsed.sha256, expected_policy_sha256)
    _require(type(profile_id) is str and profile_id in parsed.document["profiles"], "runtime_profile_unqualified")
    profile = parsed.document["profiles"][profile_id]
    _require(host_os == profile["host_os"], "runtime_metadata_host")
    platforms = set(profile["platforms"]) & _platform_facts(image_platforms) & _platform_facts(host_platforms)
    _require(type(platform) is str and platform in platforms, "runtime_metadata_platform")
    if "runtime_policy" in value:
        _require(_canonical_bytes(value["runtime_policy"]) == parsed.canonical, "runtime_metadata_policy_binding")
    value["runtime_policy"] = _thaw(parsed.document)
    raw = _canonical_bytes(value)
    _require(len(raw) <= 1048576, "runtime_metadata_size")
    return UnsignedRuntimeMetadataDraft(raw, _sha256(raw), parsed.sha256,
                                        source_sha256, schema_sha256, profile_id)
