"""Migration identity binds actual payload file names/checksums and source profile."""
import hashlib
import importlib
from pathlib import Path
import pytest


def required():
    return importlib.import_module('cortex_v2.catalogue_contract')


@pytest.mark.parametrize('defect',['valid','profile-missing-file','payload-extra-file','profile-duplicate','profile-nonliteral','ledger-missing','ledger-duplicate','ledger-drift'])
def test_payload_profile_and_ledger_identity_not_numeric_count(tmp_path,defect):
    module=required();source=tmp_path/'src';config=source/'cortex_v2/config.py';config.parent.mkdir(parents=True);migrations=tmp_path/'migrations';migrations.mkdir()
    names=['0001_core.sql','0024_context_agent_boot.sql'];hashes={}
    for name in names:
        raw=b'SELECT 1;\n';(migrations/name).write_bytes(raw);hashes[name]=hashlib.sha256(raw).hexdigest()
    profile=names.copy()
    if defect=='profile-missing-file':profile.append('0025_absent.sql')
    if defect=='payload-extra-file':(migrations/'0025_extra.sql').write_bytes(b'SELECT 2;\n')
    if defect=='profile-duplicate':profile.append(names[0])
    config.write_text('FULL_V2_MIGRATIONS = '+('dynamic()' if defect=='profile-nonliteral' else repr(tuple(profile)))+'\n')
    receipt={'migrations':[dict(migration=n,checksum=d) for n,d in hashes.items()]}
    if defect=='ledger-missing':receipt['migrations'].pop()
    if defect=='ledger-duplicate':receipt['migrations'].append(receipt['migrations'][0].copy())
    if defect=='ledger-drift':receipt['migrations'][0]['checksum']='f'*64
    if defect=='valid':
        assert module.migration_identity(source,migrations)==hashes
        assert module.validate_migration_receipt(receipt,source,migrations)==hashes
    else:
        with pytest.raises((ValueError,RuntimeError)):
            module.validate_migration_receipt(receipt,source,migrations)


@pytest.mark.parametrize('name',['public.foreign','cortex_unknown.records','cortex_core.records.extra','cortex_context.bad-name'])
def test_unsupported_namespaces_and_relation_names_stay_refused(name):
    assert not required().relation_name(name)
