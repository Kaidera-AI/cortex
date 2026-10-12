"""Raw CLI receipt gate must precede parser and candidate readers; inert fixtures."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture import canonical, digest
import test_native_policy_unit2_trust_red as trust
from cortex_core.cli import release
from cortex_core.release import artifacts
from cortex_core.release.custody import RootReader


class TrustCliOrder(unittest.TestCase):
    def test_TRUST002_raw_cli_native_receipt_rejected_before_inspection(self):
        value, inputs, receipt, outputs, evidence, policy = trust.native_case()
        with tempfile.TemporaryDirectory(prefix='r01-u2-cli-order-') as directory:
            root=Path(directory)
            for dirname,storage in [('inputs',inputs),('outputs',outputs),('evidence',evidence)]:
                for path,raw in storage.items():
                    destination=root/dirname/path;destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(raw);destination.chmod(0o644)
            for name,obj in [('input',value),('receipt',receipt),('policy',policy)]:
                (root/(name+'.json')).write_bytes(canonical(obj))
            calls=[]
            class Reader(RootReader):
                def __init__(self,path,limit):
                    if str(path) in (str(root/'outputs'),str(root/'evidence')):calls.append(str(path))
                    super().__init__(path,limit)
            argv=['verify-build-receipt','--input',str(root/'input.json'),'--input-sha256',digest(value),
                  '--input-root',str(root/'inputs'),'--policy',str(root/'policy.json'),'--policy-record-sha256',digest(policy),
                  '--receipt',str(root/'receipt.json'),'--receipt-sha256',digest(receipt),
                  '--output-root',str(root/'outputs'),'--evidence-root',str(root/'evidence'),
                  '--decision',str(root/'decision.json')]
            with patch.object(release,'RootReader',Reader),patch.object(release,'parse_build_receipt',wraps=release.parse_build_receipt) as parser,patch.object(artifacts,'_check_pyc',wraps=artifacts._check_pyc) as cache,contextlib.redirect_stdout(io.StringIO()):
                code=release.main(argv)
            self.assertEqual(code,2,'independent native receipt digest must refuse')
            self.assertEqual((parser.call_count,len(calls),cache.call_count),(0,0,0),'exact native acceptance precedes CLI parser/readers/cache')


if __name__=='__main__':unittest.main(verbosity=2)
