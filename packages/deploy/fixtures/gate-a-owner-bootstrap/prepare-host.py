#!/usr/bin/env python3
"""TEST-only fresh owner bootstrap; administrator prepares, kos installs."""
import argparse
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--host', required=True)
p.add_argument('--known-hosts', type=Path, required=True)
p.add_argument('--key', type=Path, required=True)
a = p.parse_args()
if not a.known_hosts.is_file() or not a.key.is_file():
    raise SystemExit('verified SSH bindings required')
opts = ['-i', str(a.key), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=' + str(a.known_hosts)]
admin = 'rocky@' + a.host
owner = 'kos@' + a.host
base = Path(__file__).parent
subprocess.run(['ssh', *opts, admin, 'set -eu; if ! test "$(uname -m)" = x86_64; then exit 2; fi; if ! test "$(getenforce)" = Enforcing; then exit 2; fi; umask 077; if test -e /home/rocky/gate-a-bootstrap; then exit 2; fi; if test -L /home/rocky/gate-a-bootstrap; then exit 2; fi; mkdir -m 700 /home/rocky/gate-a-bootstrap'], check=True)
subprocess.run(['scp', *opts, str(base / 'new-owner-setup.sh'), admin + ':/home/rocky/gate-a-bootstrap/'], check=True)
subprocess.run(['ssh', *opts, admin, 'bash /home/rocky/gate-a-bootstrap/new-owner-setup.sh'], check=True)
subprocess.run(['ssh', *opts, owner, 'set -eu; umask 077; if test -e /home/kos/gate-a-bootstrap; then exit 2; fi; if test -L /home/kos/gate-a-bootstrap; then exit 2; fi; mkdir -m 700 /home/kos/gate-a-bootstrap'], check=True)
for name in ['homebrew-install.sh', 'host-tools-setup.sh']:
    subprocess.run(['scp', *opts, str(base / name), owner + ':/home/kos/gate-a-bootstrap/'], check=True)
subprocess.run(['ssh', *opts, owner, 'bash /home/kos/gate-a-bootstrap/host-tools-setup.sh'], check=True)
