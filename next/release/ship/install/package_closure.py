#!/usr/bin/env python3
"""Stage the SOLE role-catalog validator into the Core closure as cortex_ship.install.roles (J06 interface; fixed namespace, no path from config).
Copies the SAME roles.py and the SAME runtime-operations-v1.json next to it (the policy source), plus empty package markers, and writes a digest receipt.
Pure Python, standard library only: the closure is identical for linux/amd64 and linux/arm64. usage: package_closure.py --out DIR (empty or absent)."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEXT = HERE.parents[2]
CONTRACT = NEXT / 'contracts/runtime/runtime-operations-v1.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    out = Path(ap.parse_args().out)
    if out.exists() and any(out.iterdir()):
        print(json.dumps({'reason': 'out_not_empty', 'unit': 'package_closure'}), file=sys.stderr)
        sys.exit(3)
    pkg = out / 'cortex_ship/install'
    pkg.mkdir(parents=True, exist_ok=True)
    files = {'cortex_ship/__init__.py': b'"""Kaidera ship closure namespace (J06): installed validators only."""\n',
             'cortex_ship/install/__init__.py': b'',
             'cortex_ship/install/roles.py': (HERE / 'roles.py').read_bytes(),
             'cortex_ship/install/provision.py': (HERE / 'provision.py').read_bytes(),
             'cortex_ship/install/runtime-operations-v1.json': CONTRACT.read_bytes()}
    for rel, data in files.items():
        (out / rel).write_bytes(data)
    receipt = {'schema': 'cortex.ship.closure.v1', 'namespace': 'cortex_ship.install.roles', 'platform_independent': True,
               'source_roles_sha256': sha(files['cortex_ship/install/roles.py']), 'source_provision_sha256': sha(files['cortex_ship/install/provision.py']), 'contract_sha256': sha(files['cortex_ship/install/runtime-operations-v1.json']),
               'files': [{'path': rel, 'sha256': sha(data)} for rel, data in sorted(files.items())]}
    (out / 'cortex_ship/receipt.json').write_text(json.dumps(receipt, indent=1, sort_keys=True) + '\n')


main()
