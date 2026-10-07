"""Offline host closure pin refusals."""
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import tarfile
import io

SPEC=importlib.util.spec_from_file_location('host',Path(__file__).with_name('build-manual-host.py'))
host=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(host)

class HostTests(unittest.TestCase):
    def setUp(self):
        self.inputs=json.loads(Path(__file__).with_name('manual-host-inputs.json').read_text())

    def test_internal_parent_symlink_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/'python.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                entry=tarfile.TarInfo('python/share/terminfo/a/target');entry.size=1
                tar.addfile(entry,io.BytesIO(b'x'))
                link=tarfile.TarInfo('python/share/terminfo/b/link');link.type=tarfile.SYMTYPE;link.linkname='../a/target';tar.addfile(link)
            host.unpack(archive,root/'stage')
            self.assertEqual((root/'stage/python/share/terminfo/b/link').read_bytes(),b'x')

    def test_escaping_runtime_symlink_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=root/'python.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                link=tarfile.TarInfo('python/bin/escape');link.type=tarfile.SYMTYPE;link.linkname='../../../outside';tar.addfile(link)
            with self.assertRaises(ValueError):host.unpack(archive,root/'stage')

    def test_complete_native_pin_set(self):
        host.validate(self.inputs)

    def test_no_sdist_or_unhashed_input(self):
        for value in ('bad', 'a'*63):
            self.inputs['wheels'][0]['sha256']=value
            with self.assertRaises(ValueError):host.validate(self.inputs)
        self.setUp();self.inputs['wheels'][0]['filename']='podman-compose.tar.gz'
        with self.assertRaises(ValueError):host.validate(self.inputs)

    def test_transitive_closure_mandatory(self):
        self.inputs['wheels']=[x for x in self.inputs['wheels'] if x['name']!='cffi']
        with self.assertRaises(ValueError):host.validate(self.inputs)

    def test_insecure_acquisition_refuses(self):
        self.inputs['python']['url']='http://example.test/python.tar.gz'
        with self.assertRaises(ValueError):host.validate(self.inputs)

if __name__=='__main__':unittest.main()
