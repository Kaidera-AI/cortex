"""Real host startup fixture, not a native image qualification."""

import importlib.util
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from cortex_core.release.build import audit


class PythonStartup(unittest.TestCase):
    def test_checked_cache_is_used_with_actual_site_and_pth_startup(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = root / "r02_pth_module.py"
            raw = b'VALUE = "owned-fixture"\n'
            source.write_bytes(raw)
            cache = Path(importlib.util.cache_from_source(str(source)))
            py_compile.compile(
                str(source),
                cfile=str(cache),
                dfile=str(source),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
            (root / "startup.pth").write_text(
                'import sys; sys.r02_owned_pth = "applied"\n'
            )
            before = cache.read_bytes()
            code = (
                "import json,site,sys;site.addsitedir("
                + repr(str(root))
                + ');import r02_pth_module as m;print(json.dumps({"value":m.VALUE,"pth":sys.r02_owned_pth,"cache":m.__cached__}))'
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                capture_output=True,
                timeout=10,
                env=dict(os.environ, PYTHONHASHSEED="0"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(
                report,
                {"value": "owned-fixture", "pth": "applied", "cache": str(cache)},
            )
            self.assertEqual(cache.read_bytes(), before)
            self.assertTrue(audit.audit_cache(before, raw, str(source)))

    def test_pth_source_change_cannot_reuse_prestartup_cache_audit(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = root / "r02_mutated_pth_module.py"
            original = b'VALUE = "before-startup"\n'
            changed = b'VALUE = "changed-at-startup"\n'
            source.write_bytes(original)
            cache = Path(importlib.util.cache_from_source(str(source)))
            py_compile.compile(
                str(source),
                cfile=str(cache),
                dfile=str(source),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
            checked = cache.read_bytes()
            (root / "mutation.pth").write_text(
                "import pathlib; pathlib.Path("
                + repr(str(source))
                + ").write_bytes("
                + repr(changed)
                + ")\n"
            )
            code = (
                "import site;site.addsitedir("
                + repr(str(root))
                + ");import r02_mutated_pth_module as m;print(m.VALUE)"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                capture_output=True,
                timeout=10,
                env=dict(os.environ, PYTHONHASHSEED="0"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"changed-at-startup\n")
            self.assertEqual(cache.read_bytes(), checked)
            with self.assertRaises(ValueError):
                audit.audit_cache(checked, source.read_bytes(), str(source))
