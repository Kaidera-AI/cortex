"""Shared, pure fixtures for the provision_roles tests (no PostgreSQL): the five purposes, stand-ins for the Core carrier views, a throwaway vault."""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
NEXT = HERE.parents[3]
FIVE = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
SERVICE = ('api_request', 'probe')
FINITE = ('installer_enrollment', 'migrator', 'bootstrap')
REQUEST_GROUP = 'kaidera-runtime-core-request'
INSTALLER = 'kaidera-runtime-core-installer'
# Stand-ins that mirror the published Core carrier (runtime/bootstrap_config.py: Role(login, secret_ref), BootstrapConfig.roles/.vault_root).
# A validated config admits only the purposes of ITS mode, so the five are gathered from the seed, serve and migrate views of the ONE config file.
LOGINS = {'bootstrap': 'cx_boot', 'migrator': 'cx_migrate', 'api_request': 'cx_api', 'probe': 'cx_probe', 'installer_enrollment': INSTALLER}
MODES = {'seed': ('bootstrap', 'installer_enrollment', 'api_request'), 'serve': ('api_request', 'probe'), 'migrate': ('migrator',)}


def config(vault, logins=None, refs=None):
    logins = dict(LOGINS, **(logins or {}))
    refs = dict({n: f'secrets/cortex/{n}-secret' for n in FIVE}, **(refs or {}))
    return [SimpleNamespace(vault_root=str(vault), mode=m,
                            roles={p: SimpleNamespace(login=logins[p], secret_ref=refs[p]) for p in ps}) for m, ps in MODES.items()]


def make_vault():
    vault = Path(tempfile.mkdtemp(prefix='nix-vault-'))
    (vault / 'secrets/cortex').mkdir(parents=True)
    values = {}
    for n in FIVE:
        values[n] = f'pw-{n}-' + os.urandom(6).hex()
        (vault / f'secrets/cortex/{n}-secret').write_text(values[n] + '\n')
    return vault, values


def contract_roles():
    return json.loads((NEXT / 'contracts/runtime/runtime-operations-v1.json').read_text())['roles']
