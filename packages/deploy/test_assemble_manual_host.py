import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('assemble',Path(__file__).with_name('assemble-manual-host.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class ClosureTests(unittest.TestCase):
    def test_resolved_dynamic_libraries_and_loader(self):
        output="linux-vdso.so.1 (0x1)\nlibc.so.6 => /lib64/libc.so.6 (0x2)\n/lib64/ld-linux-x86-64.so.2 (0x3)\n"
        self.assertEqual(a.library_paths(output),{'/lib64/libc.so.6','/lib64/ld-linux-x86-64.so.2'})

    def test_missing_library_refuses(self):
        with self.assertRaises(ValueError):a.library_paths('libgcc_s.so.1 => not found')

    def test_native_header_requires_amd64(self):
        header=b'\x7fELF\x02\x01'+bytes(12)+b'\x3e\x00'
        a.native_header(header)
        with self.assertRaises(ValueError):a.native_header(header[:18]+b'\xb7\x00')

if __name__=='__main__':unittest.main()
