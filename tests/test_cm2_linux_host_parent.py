"""Actual freezer parent aliases/modes refuse before native qualification."""
from pathlib import Path

import pytest

from test_cm2_linux_host_freeze import product
from test_linux_builder_contract import archive, elf, versions


@pytest.mark.parametrize('case', ['symlink', 'writable', 'special', 'valid'])
def test_actual_native_output_parent_custody_before_qualification(case, tmp_path, monkeypatch):
    module = product(monkeypatch)
    payload = archive([('libpython3.12.so.1.0', elf())])
    calls = []
    parent = tmp_path / 'bin'
    foreign = tmp_path / 'PUBLIC-foreign-bin'

    def run(args, *, read=False):
        if 'PyInstaller' in args:
            if not parent.exists():
                if case == 'symlink':
                    foreign.mkdir()
                    parent.symlink_to(foreign, target_is_directory=True)
                else:
                    parent.mkdir()
                    parent.chmod({'writable': 0o777, 'special': 0o2755, 'valid': 0o755}[case])
            name = next(a.split('=', 1)[1] for a in args if a.startswith('--name='))
            binary = parent / name
            binary.write_bytes(payload)
            binary.chmod(0o700)
            calls.append('freeze')
            return ''
        assert 'PyInstaller.utils.cliutils.archive_viewer' in args and read
        calls.append('viewer')
        return 'PUBLIC actual-byte inventory'

    def native(args, *, read=False):
        calls.append('qualification')
        if args[0] == 'readelf':
            return versions('2.35')
        assert args == [str(parent / Path(args[0]).name), '--help']
        return ''

    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module, 'run_linux_qualification', native)
    if case == 'valid':
        assert set(module.freeze_linux_cm2_programs(tmp_path)) == {'cortex', 'cortex-agent'}
        assert calls.count('viewer') == 2
    else:
        with pytest.raises(RuntimeError):
            module.freeze_linux_cm2_programs(tmp_path)
        assert calls == ['freeze', 'freeze']
        assert not (tmp_path / 'cm2-host-archive-inventory.txt').exists()
        assert not (tmp_path / 'agent-archive-inventory.txt').exists()
    for name in ('cortex', 'cortex-agent'):
        assert (parent / name).read_bytes() == payload
    if case == 'symlink':
        assert parent.is_symlink() and parent.readlink() == foreign
    else:
        assert parent.lstat().st_mode & 0o7777 == {'writable': 0o777, 'special': 0o2755, 'valid': 0o755}[case]
