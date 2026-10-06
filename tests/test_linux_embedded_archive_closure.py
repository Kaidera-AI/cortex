"""Additive closure checks for ELF hidden under an archive payload name."""
import io
import zipfile
import pytest
from test_linux_builder_contract import archive, elf, load, versions

def zipped(name,value):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as stream:stream.writestr(name,value)
    return out.getvalue()

@pytest.mark.parametrize('kind',['valid','higher-glibc','wrong-arch','bad-zip','traversal','too-deep'])
def test_every_embedded_elf_in_archive_payloads_is_checked(tmp_path,monkeypatch,kind):
    module=load('linux_binaries.py',monkeypatch);binary=tmp_path/'cortex-test'
    item=elf(183) if kind=='wrong-arch' else elf()+ (b'HIGHER' if kind=='higher-glibc' else b'')
    payload=zipped('../escape' if kind=='traversal' else 'odd/no-extension',item)
    if kind=='bad-zip':payload=b'PK\x03\x04broken'
    if kind=='too-deep':
        for _ in range(5):payload=zipped('nested.archive',payload)
    binary.write_bytes(archive([('opaque-resource',payload)]));calls=[]
    def run(args,*,read=False):
        calls.append(args)
        if args[0]=='readelf':
            from pathlib import Path
            return versions('2.36' if Path(args[-1]).read_bytes().endswith(b'HIGHER') else '2.35')
        assert args==[str(binary),'--help'];return ''
    if kind=='valid':
        result=module.verify_program(binary,run=run)
        assert result['embedded_elf_count']==1 and calls[-1]==[str(binary),'--help']
    else:
        with pytest.raises(RuntimeError):module.verify_program(binary,run=run)
        assert [str(binary),'--help'] not in calls
