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
