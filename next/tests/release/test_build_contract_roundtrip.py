import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from fixture import canonical, digest
from receipt_fixture import receipt_fixture
from test_unit1_red import ROOT


class Roundtrip(unittest.TestCase):
    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m', 'cortex_core.cli.release', *map(str,args)], env=env,
                              capture_output=True, timeout=20)

    def test_cli_and_api_consume_identical_input_and_receipt_bytes(self):
        try:
            cli = importlib.import_module('cortex_core.cli.release')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.cli.release': raise
            cli = None
        self.assertIsNotNone(cli, 'product release CLI absent')
        value, inputs, receipt, outputs, evidence, policy = receipt_fixture()
        with tempfile.TemporaryDirectory(prefix='r01-fixture-') as temporary:
            root = Path(temporary)
            for directory, files in (('inputs',inputs),('outputs',outputs),('evidence',evidence)):
                for path, raw in files.items():
                    destination = root / directory / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(raw)
                    destination.chmod(420)
            for name, document in (('input', value), ('receipt', receipt), ('policy', policy)):
                (root / (name+'.json')).write_bytes(canonical(document))
            admission = self.cli('admit-build', '--input',root/'input.json','--input-sha256',digest(value),
                                 '--input-root',root/'inputs','--policy',root/'policy.json',
                                 '--policy-record-sha256',digest(policy),'--decision',root/'admission.json')
            self.assertEqual(admission.returncode, 0, admission.stderr)
            self.assertEqual(json.loads((root/'admission.json').read_bytes())['input_sha256'], digest(value))
            verification = self.cli('verify-build-receipt','--input',root/'input.json','--input-sha256',digest(value),
                                     '--input-root',root/'inputs','--policy',root/'policy.json','--policy-record-sha256',digest(policy),
                                     '--receipt',root/'receipt.json','--receipt-sha256',digest(receipt),
                                     '--output-root',root/'outputs','--evidence-root',root/'evidence','--decision',root/'verified.json')
            self.assertEqual(verification.returncode,0,verification.stderr)
            result=json.loads((root/'verified.json').read_bytes())
            self.assertEqual(result['receipt_sha256'],digest(receipt))
            self.assertFalse(result['native_qualified'])
            self.assertEqual(result['input_sha256'],digest(value))
            # One changed external record cannot reuse the CLI decision.
            refusal=self.cli('admit-build','--input',root/'input.json','--input-sha256','f'*64,
                             '--input-root',root/'inputs','--policy',root/'policy.json','--policy-record-sha256',digest(policy),
                             '--decision',root/'refusal.json')
            self.assertEqual(refusal.returncode,2)
            self.assertEqual(json.loads((root/'refusal.json').read_bytes())['decision'],'refused')


if __name__=='__main__': unittest.main(verbosity=2)
