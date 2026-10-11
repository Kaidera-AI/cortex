import importlib.util
import marshal
import struct
import unittest
from cortex_core.release.build import audit, policy


class Bytecode(unittest.TestCase):
    def test_actual_compiled_header_source_recursive_body_final_filename(self):
        source = b'def nested():\n return {"values": {1,2,3}}\n'
        filename = "/app/module.py"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 3)
            + importlib.util.source_hash(source)
            + marshal.dumps(compile(source, filename, "exec"))
        )
        self.assertTrue(audit.audit_cache(raw, source, filename))
        self.assertEqual(
            audit.audit_all_caches(
                [{"source": "module.py", "filename": filename, "bytes": raw}],
                {"module.py": source},
            )["cache_count"],
            1,
        )
        with self.assertRaises(ValueError):
            audit.audit_all_caches([], {"module.py": source})

    def test_real_checked_hash_flags_required(self):
        source = b"VALUE=1\n"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 0)
            + b"\0" * 8
            + marshal.dumps(compile(source, "/app/module.py", "exec"))
        )
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, source, "/app/module.py")

    def test_metadata_and_offline_inventory_positive(self):
        self.assertTrue(
            audit.compare_metadata(
                {"uid": 10001, "mode": 0o444, "xattrs": {}},
                {"uid": 10001, "mode": 0o444, "xattrs": {}},
            )
        )
        self.assertTrue(
            audit.validate_offline_assets(
                {"required": "a" * 64}, {"required": "a" * 64}
            )
        )
        self.assertTrue(
            policy.verify_native(
                {"os": "linux", "arch": "aarch64", "uid": 10001, "rootless": True},
                "linux/arm64",
            )
        )

    def test_trailing_cache_payload_refused(self):
        source = b"VALUE=1\n"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 3)
            + importlib.util.source_hash(source)
            + marshal.dumps(compile(source, "/app/module.py", "exec"))
            + b"UNBOUND_TRAILING_DATA"
        )
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, source, "/app/module.py")

class ConstantMutation(unittest.TestCase):
    def test_equal_frozenset_values_with_different_types_are_not_equal_code(self):
        source = b'VALUES = {1, 2, 3}\n'
        filename = '/app/module.py'
        code = compile(source, filename, 'exec')
        mutant = code.replace(co_consts=tuple(frozenset({True, 2, 3}) if isinstance(value, frozenset) else value for value in code.co_consts))
        raw = importlib.util.MAGIC_NUMBER + struct.pack('<I', 3) + importlib.util.source_hash(source) + marshal.dumps(mutant)
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, source, filename)
