"""Cortex runtime minimum, with a visible dated list of known bad versions."""
from datetime import date
import re

POLICY = {
    'minimum_version': '6.0.2',
    'minimum_reason': 'Cortex release lifecycle minimum accepted by Kai r197; client and server both checked.',
    'denylist': {'as_of': '2026-10-06', 'entries': []},
}


def validate_versions(value: str, *, policy=POLICY) -> dict:
    def version(text):
        if not isinstance(text, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', text):
            raise ValueError('cortex_podman_unsupported: stable numeric version required')
        return tuple(int(part) for part in text.split('.'))
    try:
        minimum = version(policy['minimum_version'])
        if minimum < (6, 0, 2) or not isinstance(policy['minimum_reason'], str) or not policy['minimum_reason'].strip():
            raise ValueError('cortex_podman_unsupported: invalid minimum policy')
        denied = policy['denylist']
        as_of = date.fromisoformat(denied['as_of'])
        if not isinstance(denied['entries'], list):
            raise ValueError('cortex_podman_unsupported: invalid denylist')
        entries = {}
        for entry in denied['entries']:
            version(entry['version'])
            if (date.fromisoformat(entry['date']) > as_of or entry['version'] in entries
                    or not isinstance(entry['reason'], str) or not entry['reason'].strip()):
                raise ValueError('cortex_podman_unsupported: invalid dated denial')
            entries[entry['version']] = entry
        parts = value.split()
        if len(parts) != 2 or any(version(part) < minimum for part in parts):
            raise ValueError('cortex_podman_unsupported: client/server minimum ' + policy['minimum_version'])
        for part in parts:
            if part in entries:
                entry = entries[part]
                raise ValueError('cortex_podman_denied: ' + part + ' (' + entry['date'] + '): ' + entry['reason'])
    except (KeyError, TypeError, AttributeError):
        raise ValueError('cortex_podman_unsupported: invalid version or policy') from None
    return {'client_version': parts[0], 'server_version': parts[1],
            'minimum_version': policy['minimum_version'], 'denylist_as_of': denied['as_of']}


def validate_local_version(value: str, *, policy=POLICY) -> dict:
    """Local Podman's Client.Version describes its in-process ABI engine too.

    The native ABI has no separate Server record. Do not apply a remote
    Client/Server template or claim a reported remote version here.
    """
    if not isinstance(value, str) or len(value.split()) != 1:
        raise ValueError('cortex_podman_unsupported: one native local version required')
    result = validate_versions(value + ' ' + value, policy=policy)
    result['engine_version'] = result.pop('server_version')
    result['transport'] = 'native-local-abi'
    return result


def validate_linuxbrew_provider(metadata: dict, engine_version: str) -> dict:
    """Bind the current core formula and installed stock keg to observed ABI."""
    try:
        formulae = metadata['formulae']
        if not isinstance(formulae, list) or len(formulae) != 1:
            raise ValueError
        formula = formulae[0]
        revision = formula['revision']
        if (formula['name'] != 'podman' or formula['tap'] != 'homebrew/core'
                or formula['versions']['stable'] != engine_version
                or type(revision) is not int or revision < 0):
            raise ValueError
        validate_local_version(engine_version)
        keg = engine_version + ('_' + str(revision) if revision else '')
        installed = formula['installed']
        if not isinstance(installed, list):
            raise ValueError
        matches = [item for item in installed if item['version'] == keg]
        if (formula['linked_keg'] != keg or len(matches) != 1
                or matches[0]['poured_from_bottle'] is not True):
            raise ValueError
        bottle = formula['bottle']['stable']['files']['x86_64_linux']
        checksum = bottle['sha256']
        if (not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                or bottle['url'] != 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + checksum):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('cortex_podman_provider_invalid: current Linuxbrew stock bottle/version binding required') from None
    return {'provider': 'linuxbrew', 'formula': 'homebrew/core/podman', 'version': engine_version,
            'linked_keg': keg, 'poured_from_bottle': True, 'bottle_sha256': checksum,
            'bottle_url': bottle['url'], 'checksum_verifier': 'Homebrew',
            'scope': 'current formula metadata and installed stock keg; Homebrew verifies downloaded bottle bytes'}
