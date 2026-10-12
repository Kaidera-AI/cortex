"""Separately frozen mixed base/generated and ordered-layer policy controls."""
import re
import unittest
import native_matrix_fixture as matrix


class LayerMatrix(unittest.TestCase):
    def positive(self,module,arch,channel):
        case=matrix.family(module,arch)
        profile=matrix.json.loads(case[1][case[0]['descriptor']['path']])['release_policy']
        base=profile['layer_policy']['base']['inventory']
        self.assertTrue(any(r['uid']==0 and r['path']=='etc/shadow' for r in base))
        self.assertEqual(profile['history_to_layers'],[0,1,None,2])
        try:result=channel(case)
        except ValueError as error:self.fail('required mixed-base ordered-layer positive refused: '+str(error))
        self.assertEqual(result['decision'],'verified')
        self.assertFalse(result['native_qualified'])

    def negative(self,fault,reason,channel):
        with self.assertRaisesRegex(ValueError,'^'+re.escape(reason)+'$'):
            channel(matrix.family(fault=fault))


for module in ('core','api'):
    for arch in ('amd64','arm64'):
        for channel_name,channel in [('api',matrix.api),('cli',matrix.cli)]:
            def positive(self,module=module,arch=arch,channel=channel):self.positive(module,arch,channel)
            setattr(LayerMatrix,'test_positive_'+module+'_'+arch+'_'+channel_name,positive)

LAYER_NEGATIVES={
    'base_content':'base_layer_digest',
    'base_manifest_identity':'base_manifest_identity',
    'base_inventory_metadata':'base_inventory_metadata',
    'static_account':'base_layer_digest',
    'generated_account':'generated_account_log_cache',
    'transition_predecessor':'layer_transition_predecessor',
    'layer_order':'layer_transition_predecessor',
    'member_order':'layer_member_order',
    'history':'oci_history_policy',
    'empty_layer_position':'oci_history_layer_mapping',
    'cross_platform_base':'base_layer_digest',
}
for fault,reason in LAYER_NEGATIVES.items():
    for channel_name,channel in [('api',matrix.api),('cli',matrix.cli)]:
        def negative(self,fault=fault,reason=reason,channel=channel):self.negative(fault,reason,channel)
        setattr(LayerMatrix,'test_negative_'+fault+'_'+channel_name,negative)


if __name__=='__main__':unittest.main(verbosity=2)
