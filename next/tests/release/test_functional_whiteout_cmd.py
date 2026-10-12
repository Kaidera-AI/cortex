"""Mike frozen desired8 bodies; portable wrapper only, inert producer unchanged."""
from pathlib import Path
import importlib.util
import unittest
import native_matrix_fixture as matrix
FOLDER=Path(__file__).resolve().parent/'functional_controls'
spec=importlib.util.spec_from_file_location('r01_frozen_case_factory',FOLDER/'case_factory.py')
case_factory=importlib.util.module_from_spec(spec);spec.loader.exec_module(case_factory)
case_factory.ROOT=FOLDER/'captured'
saved={label:case_factory.case(label) for label in ['baseline','whiteout_cmd_input','cmd_unreadable','cmd_parent_unsearchable']}

class DesiredRelations(unittest.TestCase):
 def baseline(self,channel):
  result=getattr(matrix,channel)(saved['baseline']);self.assertEqual(result['decision'],'verified');self.assertFalse(result['native_qualified'])
 def refuse(self,label,channel):
  with self.assertRaises(ValueError,msg=label+' must refuse despite syntactically accepted profile'):
   getattr(matrix,channel)(saved[label])
for channel in ['api','cli']:
 def positive(self,channel=channel):self.baseline(channel)
 setattr(DesiredRelations,'test_coherent_positive_'+channel,positive)
 for label in ['whiteout_cmd_input','cmd_unreadable','cmd_parent_unsearchable']:
  def negative(self,label=label,channel=channel):self.refuse(label,channel)
  setattr(DesiredRelations,'test_must_refuse_'+label+'_'+channel,negative)

if __name__=="__main__":unittest.main(verbosity=2)
