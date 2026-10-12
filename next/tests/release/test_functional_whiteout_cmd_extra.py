"""Readable root-owned positive and opaque-marker intended-reason controls."""
import unittest
from unittest.mock import patch
import native_matrix_fixture as matrix
import test_functional_whiteout_cmd as frozen


def extended_case(kind):
    factory=frozen.case_factory
    original=factory.load_defs
    def load(relative,names,namespace,*,hook=False):
        if hook:
            prior=namespace['_review_hook']
            def extend(value,inputs,base,first,second,runtime,profile):
                prior(value,inputs,base,first,second,runtime,profile)
                if kind=='root_readable':
                    config=next(r for r in first if r['path']=='app/config.json')
                    config.update(uid=0,gid=0,mode=0o644)
                    profile['layer_policy']['generated']['one']=namespace['inventory'](first)
                else:
                    second.append(namespace['row']('app/.wh..wh..opq',b''))
                    profile['layer_policy']['generated']['two']=namespace['inventory'](second)
                    profile['layer_policy']['member_order']['two']=[r['path'] for r in second]
            namespace['_review_hook']=extend
        return original(relative,names,namespace,hook=hook)
    with patch.object(factory,'load_defs',load):return factory.case('baseline')


class ExtraFunctional(unittest.TestCase):
    def test_root_owned_readable_cmd_input_api(self):
        self.assertFalse(matrix.api(extended_case('root_readable'))['native_qualified'])
    def test_root_owned_readable_cmd_input_cli(self):
        self.assertFalse(matrix.cli(extended_case('root_readable'))['native_qualified'])
    def test_opaque_marker_api_has_exact_reason(self):
        with self.assertRaisesRegex(ValueError,'^profile_whiteout_denied$'):matrix.api(extended_case('opaque'))
    def test_opaque_marker_cli_has_exact_reason(self):
        with self.assertRaisesRegex(ValueError,'^profile_whiteout_denied$'):matrix.cli(extended_case('opaque'))
    def test_unreadable_cmd_has_exact_reason(self):
        with self.assertRaisesRegex(ValueError,'^runtime_cmd_access$'):matrix.api(frozen.saved['cmd_unreadable'])
    def test_unsearchable_cmd_parent_has_exact_reason(self):
        with self.assertRaisesRegex(ValueError,'^runtime_cmd_access$'):matrix.api(frozen.saved['cmd_parent_unsearchable'])


if __name__=='__main__':unittest.main(verbosity=2)
