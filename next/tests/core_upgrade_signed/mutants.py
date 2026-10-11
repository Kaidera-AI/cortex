"""Apply one named signed-admission mutation to a temporary source copy."""

import hashlib
import json
from pathlib import Path
import sys


MUTATIONS = {
    'bypass_signature': (
        "    if result.returncode != 0:\n"
        "        raise UpgradeRefusal('signature_invalid')",
        "    if False:  # mutant: accept a failed signature check\n"
        "        raise UpgradeRefusal('signature_invalid')",
    ),
    'ignore_digest_pin': (
        "        if hashlib.sha256(raw).hexdigest() != pinned_digest:\n"
        "            raise UpgradeRefusal('digest_mismatch')",
        "        if False:  # mutant: skip the pinned digest\n"
        "            raise UpgradeRefusal('digest_mismatch')",
    ),
    'allow_new_writer_old_schema': (
        "        return (release, schema_version) in self.writers and not (\n"
        "            release == self.new_release and schema_version == self.old_schema)",
        "        return (release, schema_version) in self.writers  # mutant",
    ),
    'accept_missing_old_origin': (
        "            old = _manifest(_verified_bytes(old_manifest, old_signature, stable_key,\n"
        "                                            policy['old_manifest_sha256'], binary))",
        "            try:\n"
        "                old = _manifest(_verified_bytes(old_manifest, old_signature, stable_key,\n"
        "                                                policy['old_manifest_sha256'], binary))\n"
        "            except UpgradeRefusal as error:\n"
        "                if error.code != 'signature_unavailable':\n"
        "                    raise\n"
        "                old = {'release': policy['old_release'],\n"
        "                       'edition': policy['edition'], 'platform': policy['platform'],\n"
        "                       'schema_version': policy['old_schema'],\n"
        "                       'event_version': policy['old_event'],\n"
        "                       'modules': policy['old_modules']}  # mutant",
    ),
}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in MUTATIONS:
        raise RuntimeError('usage: mutants.py NAME temporary-upgrade.py')
    name, path = sys.argv[1], Path(sys.argv[2])
    source = path.read_text()
    old, new = MUTATIONS[name]
    if source.count(old) != 1:
        raise RuntimeError(f'mutation anchor count is {source.count(old)}: {name}')
    if name == 'allow_new_writer_old_schema':
        guard = "        or (new['release'], old['schema_version']) in writers):"
        if source.count(guard) != 1:
            raise RuntimeError('new-writer policy guard anchor missing')
        source = source.replace(guard, "        ):  # mutant: accept new writer on old schema")
    source = source.replace(old, new)
    compile(source, str(path), 'exec')
    path.write_text(source)
    print(json.dumps({'name': name, 'overlay_source_sha256':
                      hashlib.sha256(source.encode()).hexdigest()}, sort_keys=True))


if __name__ == '__main__':
    main()
