"""Unencodable public string input uses the same permanent decoder refusal."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from custody import Refusal
from keys import decoded_outcome

class UnicodeStopCase(unittest.TestCase):
    def test_surrogate_is_typed_decoder_refusal(self):
        try:
            decoded_outcome('\ud800')
            result='accepted'
        except Refusal as error:result=error.code
        except UnicodeError as error:result=type(error).__name__
        self.assertEqual(result,'STOP_CONSOLE_DECODE_FAILURE')

if __name__=='__main__':unittest.main(verbosity=2)
