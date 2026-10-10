import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location('log_test_producer', Path(__file__).with_name('build-manual-linux.py'))
producer = importlib.util.module_from_spec(s)
s.loader.exec_module(producer)


class ExportLogs(unittest.TestCase):
    def test_each_export_retains_distinct_stdout_stderr_and_command_exit_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            commands = []
            def engine(command, **kw):
                label = Path(command[command.index('--output')+1]).stem
                commands.append(command)
                markers = 'STDOUT-'+label+'\nSTDERR-'+label+'\n'
                if kw.get('stdout') is not None:
                    kw['stdout'].write(markers)
                target = Path(command[command.index('--output')+1])
                target.mkdir()
                (target/'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
                (target/'index.json').write_text('{"schemaVersion":2,"manifests":[]}')
                return subprocess.CompletedProcess(command, 0, stdout=markers)
            logs = []
            for label in ['A', 'B']:
                archive = root/(label+'.oci.tar')
                with patch.object(producer.subprocess, 'run', side_effect=engine):
                    receipt = producer.export_image(['/fake/podman'], 'same-image', archive, {})
                log = archive.with_suffix('.export.log')
                self.assertTrue(log.is_file(), 'separate raw export log missing: '+label)
                raw = log.read_bytes()
                self.assertEqual(raw, ('STDOUT-'+label+'.oci\nSTDERR-'+label+'.oci\n').encode())
                self.assertEqual(receipt['save_command'], commands[-1])
                self.assertEqual(receipt['save_exit_code'], 0)
                self.assertEqual(receipt['raw_log']['path'], str(log))
                self.assertEqual(receipt['raw_log']['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(receipt['raw_log']['size'], len(raw))
                self.assertEqual(json.loads(archive.with_suffix('.export-verification.json').read_text()), receipt)
                logs.append(raw)
            self.assertNotEqual(logs[0], logs[1])
            self.assertEqual((root/'A.oci.tar').read_bytes(), (root/'B.oci.tar').read_bytes())


if __name__ == '__main__':
    unittest.main()
