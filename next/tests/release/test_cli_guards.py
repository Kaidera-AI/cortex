import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fixture import canonical, digest
from receipt_fixture import receipt_fixture
from cortex_core.cli import release
from cortex_core.release.custody import RootReader


class CliGuards(unittest.TestCase):
    def files(self, root):
        value, inputs, receipt, outputs, evidence, policy = receipt_fixture()
        for dirname, records in (('inputs', inputs), ('outputs', outputs), ('evidence', evidence)):
            for path, raw in records.items():
                destination=root/dirname/path;destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(raw);destination.chmod(420)
        for name, obj in (('input',value),('receipt',receipt),('policy',policy)):
            (root/(name+'.json')).write_bytes(canonical(obj))
        return value,receipt,policy

    def args(self, root, value, policy):
        return ['admit-build','--input',str(root/'input.json'),'--input-sha256',digest(value),
                '--input-root',str(root/'inputs'),'--policy',str(root/'policy.json'),
                '--policy-record-sha256',digest(policy),'--decision',str(root/'decision.json')]

    def test_wrong_policy_primitive_is_structured_refusal(self):
        with tempfile.TemporaryDirectory(prefix='r01-cli-') as directory:
            root=Path(directory);value,receipt,policy=self.files(root)
            for wrong in ([],None,1,True,'public'):
                (root/'policy.json').write_bytes(canonical(wrong))
                self.assertEqual(release.main(self.args(root,value,wrong)),2)

    def test_failed_evidence_root_closes_already_open_readers(self):
        with tempfile.TemporaryDirectory(prefix='r01-cli-') as directory:
            root=Path(directory);value,receipt,policy=self.files(root);opened=[]
            class Reader(RootReader):
                def __init__(self, path, limit):
                    if str(path)==str(root/'evidence'): raise OSError('synthetic refused root')
                    super().__init__(path,limit);opened.append(self)
            args=self.args(root,value,policy);args[0]='verify-build-receipt'
            args+=['--receipt',str(root/'receipt.json'),'--receipt-sha256',digest(receipt),
                   '--output-root',str(root/'outputs'),'--evidence-root',str(root/'evidence')]
            try:
                with patch.object(release,'RootReader',Reader): self.assertEqual(release.main(args),2)
                self.assertTrue(all(reader.fd is None for reader in opened))
            finally:
                for reader in opened:reader.close()


if __name__=='__main__': unittest.main(verbosity=2)
