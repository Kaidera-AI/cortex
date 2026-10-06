"""Admit independent AMD64 digest substitutions only; preserve recipe bodies."""
import hashlib
import json
import re
from pathlib import Path


def verify_recipe_parity(root: Path) -> dict:
    metadata = json.loads((root / 'deploy/release/linux-amd64-bases.json').read_text())
    pairs = [('python', 'Dockerfile', 'deploy/release/Dockerfile.linux-amd64'),
             ('postgres', 'deploy/release/Containerfile.db', 'deploy/release/Containerfile.db.linux-amd64')]
    if metadata.get('schema') != 'cortex.linux-amd64-bases.v1' or set(metadata.get('bases', {})) != {'python', 'postgres'}:
        raise RuntimeError('recipe parity: invalid base metadata')
    result = {}
    for kind, original, native in pairs:
        entry = metadata['bases'][kind]
        if (entry.get('architecture') != 'amd64' or entry.get('os') != 'linux'
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', entry.get('manifest_digest', ''))
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', entry.get('config_digest', ''))):
            raise RuntimeError('recipe parity: base is not a pinned native AMD64 manifest')
        old, new = (root / original).read_text(), (root / native).read_text()
        bases = re.findall(r'^FROM (\S+@sha256:[0-9a-f]{64})(?: AS \S+)?$', old, re.M)
        replacements = set(bases)
        expected = entry['reference'] + '@' + entry['manifest_digest']
        if len(replacements) != 1 or any(b.split('@')[0] != entry['reference'] for b in replacements):
            raise RuntimeError('recipe parity: original source version differs')
        if old.replace(bases[0], expected) != new:
            raise RuntimeError('recipe parity: body differs beyond the independent architecture digest')
        result[kind] = dict(entry, recipe_sha256=hashlib.sha256(new.encode()).hexdigest())
    return result
