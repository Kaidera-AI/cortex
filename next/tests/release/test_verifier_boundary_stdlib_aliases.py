"""Q1 (kai 16 Oct): CPython 3.12's typing registers typing.io/typing.re in sys.modules.

They are pure-Python pseudo-modules created by the ADMITTED stdlib typing.py, not
native aliases. R01 admits EXACTLY these two names when the held value is the very
object typing created (typing.io / typing.re attributes of the loaded typing module)
and the declared owner path is the admitted stdlib typing.py that module was loaded
from. Nothing else widens. SOURCE SIMULATION (the real 3.12.15 closure is the gB2/Q1
container rehearsal); fixture files are inert.
"""
from dataclasses import replace
import importlib.machinery as machinery
from types import SimpleNamespace
import unittest
from r01u3_support.startup import StartupFixture


class StdlibAliases(StartupFixture, unittest.TestCase):
    def init_typing(self):
        api = self.init_boundary()
        row = self.add_original('stdlib/typing.py', b'# INERT stdlib typing original\n', role='stdlib')
        self.typing_path = row[1]
        self.frame = replace(self.frame, files=tuple(sorted(self.files + (row,), key=lambda r: r[1])))
        self.files = self.frame.files
        self.io, self.re = object(), object()
        self.typing = SimpleNamespace(io=self.io, re=self.re)  # the loaded typing module's own attributes
        self.frame = replace(self.frame, module_aliases=(('typing.io', self.io, self.typing_path),
                                                         ('typing.re', self.re, self.typing_path)))
        self.replace_observation(modules=(*self.observation.modules,
            ('typing', self.typing_path, machinery.SourceFileLoader, ()),
            ('typing.io', None, object, ()), ('typing.re', None, object, ())),
            module_objects=(('typing', self.typing), ('typing.io', self.io), ('typing.re', self.re)))
        return api

    def test_positive_typing_pseudo_modules_bind_to_the_admitted_stdlib_typing(self):
        self.init_typing(); owner, stamp = self.check()
        self.assertEqual(owner.policy_sha256, self.frame.owner_binding[0])
        self.assertEqual(len(stamp), len(self.files))

    def test_undeclared_typing_pseudo_module_still_refuses(self):
        self.init_typing(); self.frame = replace(self.frame, module_aliases=self.frame.module_aliases[:1])
        self.refusal('member_authority_unavailable', self.check)

    def test_any_other_pseudo_module_name_refuses(self):
        self.init_typing(); other = object(); self.typing.other = other
        self.frame = replace(self.frame, module_aliases=(*self.frame.module_aliases, ('typing.other', other, self.typing_path)))
        self.replace_observation(modules=(*self.observation.modules, ('typing.other', None, object, ())),
                                 module_objects=(*self.observation.module_objects, ('typing.other', other)))
        self.refusal('required_binding_gap', self.check)

    def test_foreign_object_under_the_typing_name_refuses(self):
        self.init_typing(); foreign = object()
        self.frame = replace(self.frame, module_aliases=(('typing.io', foreign, self.typing_path), self.frame.module_aliases[1]))
        self.replace_observation(module_objects=(('typing', self.typing), ('typing.io', foreign), ('typing.re', self.re)))
        self.refusal('member_authority_unavailable', self.check)

    def test_held_object_replaced_after_bootstrap_refuses(self):
        self.init_typing()
        self.replace_observation(module_objects=(('typing', self.typing), ('typing.io', object()), ('typing.re', self.re)))
        self.refusal('member_authority_unavailable', self.check)

    def test_non_stdlib_or_other_stdlib_path_refuses(self):
        self.init_typing()
        for path in (next(r[1] for r in self.files if r[0] == 'source'),
                     next(r[1] for r in self.files if r[0] == 'stdlib' and r[1] != self.typing_path)):
            frame = self.frame
            self.frame = replace(frame, module_aliases=(('typing.io', self.io, path), frame.module_aliases[1]))
            self.refusal('required_binding_gap', self.check)
            self.frame = frame

    def test_typing_loaded_from_elsewhere_refuses(self):
        self.init_typing()
        elsewhere = next(r[1] for r in self.files if r[0] == 'stdlib' and r[1] != self.typing_path)
        self.replace_observation(modules=tuple(('typing', elsewhere, machinery.SourceFileLoader, ()) if m[0] == 'typing' else m
                                               for m in self.observation.modules))
        self.refusal('member_authority_unavailable', self.check)


if __name__ == '__main__':
    unittest.main(verbosity=2)
