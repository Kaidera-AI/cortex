import hashlib
import importlib.util
import marshal
from pathlib import Path
import struct
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('finalizer', HERE / 'finalize-build.py')
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)

class FinalizerTests(unittest.TestCase):
    def test_final_path_hash_and_identical_builds(self):
        results=[]
        for timestamp in (1234567890, 1700000000):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder); src=root/'opt/bcrg/lib/python3.13/site-packages/bin/jp.py'
                src.parent.mkdir(parents=True); src.write_text('VALUE = 42\n')
                import os, py_compile
                os.utime(src,(timestamp,timestamp))
                py_compile.compile(str(src),dfile='/tmp/pip-target-random/lib/python/jp.py')
                finalizer.finalize(root, compile_roots=['opt/bcrg'])
                caches=list(src.parent.rglob('*.pyc')); self.assertEqual(len(caches),1)
                data=caches[0].read_bytes();self.assertEqual(struct.unpack('<I',data[4:8])[0],3)
                self.assertEqual(marshal.loads(data[16:]).co_filename,'/opt/bcrg/lib/python3.13/site-packages/bin/jp.py')
                results.append(hashlib.sha256(data).hexdigest())
        self.assertEqual(*results)
    def test_cleanup_exact_paths_preserves_model_and_neighbor(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in finalizer.BYPRODUCTS:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'generated')
            keep=root/'opt/kaidera-qwen3/model.onnx';keep.parent.mkdir(parents=True);keep.write_bytes(b'weights')
            neighbor=root/'var/log/keep.log';neighbor.write_bytes(b'keep')
            finalizer.finalize(root,compile_roots=[])
            self.assertTrue(all(not (root/x).exists() for x in finalizer.BYPRODUCTS))
            self.assertEqual(keep.read_bytes(),b'weights');self.assertEqual(neighbor.read_bytes(),b'keep')
    def test_compile_failure_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'app/broken.py';p.parent.mkdir();p.write_text('def invalid(\n')
            with self.assertRaises(RuntimeError):finalizer.finalize(root,compile_roots=['app'])
    def test_dockerfile_layer_hooks(self):
        source=(HERE/'Dockerfile').read_text()
        self.assertIn('--no-compile',source)
        self.assertIn('PYTHONHASHSEED=0 /usr/local/bin/python /usr/local/libexec/kaidera/finalize-build.py',source)
        self.assertNotIn('PYTHONHASHSEED=0 /usr/local/bin/python -I',source)
        self.assertGreaterEqual(source.count('finalize-build.py --cleanup-only'),3)
        self.assertIn('USER 0\nRUN PYTHONHASHSEED=0',source)
        self.assertIn('USER 10001:10001\n\nEXPOSE',source)

if __name__=='__main__':unittest.main()
