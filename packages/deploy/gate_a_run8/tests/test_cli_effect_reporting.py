"""The CLI cannot claim no allocation after a checked effect has failed."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import run8
from custody import Refusal


class EffectReportingCase(unittest.TestCase):
    def test_post_effect_refusal_reports_unknown_allocation_state(self):
        effects=[];err=io.StringIO()
        def effect(*a,**k):effects.append(True);raise Refusal('checked_command_failed')
        argv=['execute-role','--binding','/public/B','--binding-sha256','a'*64,'--go','/public/G',
              '--go-sha256','b'*64,'--observations','/public/O','--role','launcher','--invocations','/public/I']
        with patch.object(run8,'file_row',return_value={}),patch.object(run8,'load_authority',return_value=object()),\
             patch.object(run8,'validate_release'),patch.object(run8,'ObservationStore'),patch.object(run8,'execute',side_effect=effect),contextlib.redirect_stderr(err):
            result=run8.main(argv)
        self.assertEqual(result,2);self.assertEqual(effects,[True])
        self.assertNotEqual(json.loads(err.getvalue()).get('allocation_performed'),False)
        self.assertEqual(json.loads(err.getvalue())['effects'],'SEE_APPEND_ONLY_RECEIPTS')


if __name__=='__main__':unittest.main(verbosity=2)
