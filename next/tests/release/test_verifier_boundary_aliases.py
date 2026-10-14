"""A native-generated module bindings: SOURCE SIMULATION, no native proof."""
from dataclasses import replace
import importlib.machinery as machinery
import unittest
from r01u3_support.startup import StartupFixture

class NativeAliases(StartupFixture, unittest.TestCase):
    def init_alias(self):
        api=self.init_boundary()
        self.assertIn('module_aliases',api._StartupContext.__dataclass_fields__,
                      'SURFACE_ABSENCE_BODY: independently held native alias DATA')
        self.token=object();self.native=next(r[1] for r in self.files if r[0]=='loader')
        self.frame=replace(self.frame,module_aliases=(('declared_native_alias',self.token,self.native),))
        self.replace_observation(modules=(*self.observation.modules,
            ('declared_owner_native',self.native,machinery.ExtensionFileLoader,()),
            ('declared_native_alias',None,object,())),module_objects=(('declared_native_alias',self.token),))
        return api

    def test_positive_declared_native_alias_binds_held_identity_and_original_extension(self):
        self.init_alias();owner,stamp=self.check()
        self.assertEqual(owner.policy_sha256,self.frame.owner_binding[0])
        self.assertEqual(len(stamp),len(self.files));self.assertEqual(self.transports,[])

    def test_unknown_native_alias_refuses(self):
        self.init_alias();self.frame=replace(self.frame,module_aliases=())
        self.refusal('member_authority_unavailable',self.check)

    def test_replaced_same_named_native_alias_object_refuses(self):
        self.init_alias();self.replace_observation(module_objects=(('declared_native_alias',object()),))
        self.refusal('member_authority_unavailable',self.check)

    def test_declared_alias_requires_current_loaded_extension_owner(self):
        self.init_alias();self.replace_observation(modules=tuple(r for r in self.observation.modules if r[0]!='declared_owner_native'))
        self.refusal('member_authority_unavailable',self.check)

    def test_source_file_cannot_claim_native_generated_alias_ownership(self):
        self.init_alias();source=next(r[1] for r in self.files if r[0]=='source')
        self.frame=replace(self.frame,module_aliases=(('declared_native_alias',self.token,source),))
        self.refusal('required_binding_gap',self.check)
