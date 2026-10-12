"""Separately frozen required runtime footprints; no inert executable is run."""
import re
import unittest
import native_matrix_fixture as matrix


class RuntimeMatrix(unittest.TestCase):
    def positive(self,module,arch,channel, fault=None):
        case=matrix.family(module,arch,fault=fault)
        try:result=channel(case)
        except ValueError as error:self.fail('required coherent runtime positive refused: '+str(error))
        self.assertEqual(result['decision'],'verified')
        self.assertFalse(result['native_qualified'])
        self.assertEqual(result['input_sha256'],matrix.digest(case[0]))
        self.assertEqual(result['output_sha256'],case[2]['comparison']['raw_archive_sha256'])

    def negative(self,fault,reason,channel):
        case=matrix.family(fault=fault)
        with self.assertRaisesRegex(ValueError,'^'+re.escape(reason)+'$'):
            channel(case)


for module in ('core','api'):
    for arch in ('amd64','arm64'):
        for channel_name,channel in [('api',matrix.api),('cli',matrix.cli)]:
            def positive(self,module=module,arch=arch,channel=channel):self.positive(module,arch,channel)
            setattr(RuntimeMatrix,'test_positive_'+module+'_'+arch+'_'+channel_name,positive)

for arch in ('amd64','arm64'):
    for channel_name,channel in [('api',matrix.api),('cli',matrix.cli)]:
        def positive_base(self,arch=arch,channel=channel):self.positive('api',arch,channel,fault='base_runtime_selected')
        setattr(RuntimeMatrix,'test_positive_root_owned_base_'+arch+'_'+channel_name,positive_base)

RUNTIME_NEGATIVES={
    'executable_missing':'runtime_executable_missing',
    'executable_bytes':'runtime_executable_digest',
    'executable_metadata':'runtime_executable_metadata',
    'selected_uid_traversal':'runtime_traversal_access',
    'selected_uid_execute':'runtime_execute_access',
    'relative_program':'runtime_program_resolution',
    'unsafe_link':'layer_symlink_policy',
    'cmd_input_missing':'runtime_cmd_binding',
    'cmd_wrong_recipe':'runtime_cmd_binding',
    'working_dir_missing':'runtime_working_directory',
    'working_dir_file':'runtime_working_directory',
    'working_dir_inaccessible':'runtime_working_directory_access',
}
for fault,reason in RUNTIME_NEGATIVES.items():
    for channel_name,channel in [('api',matrix.api),('cli',matrix.cli)]:
        def negative(self,fault=fault,reason=reason,channel=channel):self.negative(fault,reason,channel)
        setattr(RuntimeMatrix,'test_negative_'+fault+'_'+channel_name,negative)


if __name__=='__main__':unittest.main(verbosity=2)
