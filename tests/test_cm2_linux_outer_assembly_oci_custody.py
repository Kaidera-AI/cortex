"""Actual named OCI parser race; foreign file is public synthetic content."""
from pathlib import Path
import tarfile

from test_cm2_linux_outer_assembly_custody import ActiveAssembly
from test_cm2_linux_outer_assembly import test_literal_cm2_bundle_assembly_binds_producer_inputs_before_unsigned_success as valid_driver


def test_actual_oci_parser_uses_retained_archive_without_reopening_replaceable_name(tmp_path,monkeypatch):
    proxy=ActiveAssembly(monkeypatch)
    foreign=tmp_path/'PUBLIC-foreign-oci';foreign.write_bytes(b'PUBLIC synthetic foreign bytes; no credential\n')
    chosen=tmp_path/'image-stage/images/api.oci.tar'
    actual=tarfile.open;named_reads=[]
    def open_named(name=None,mode='r',fileobj=None,**kwargs):
        if proxy.active and name is not None and Path(name)==chosen and fileobj is None and mode=='r':
            chosen.rename(tmp_path/'PUBLIC-held-oci');chosen.symlink_to(foreign);named_reads.append(True)
        return actual(name,mode,fileobj=fileobj,**kwargs)
    monkeypatch.setattr(tarfile,'open',open_named)
    valid_driver('valid',tmp_path,proxy)
    assert named_reads==[]
    assert foreign.read_bytes()==b'PUBLIC synthetic foreign bytes; no credential\n'
