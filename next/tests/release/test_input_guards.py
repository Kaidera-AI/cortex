"""Accepted input still has to satisfy structural and actual-byte semantics."""
import unittest
import json
from fixture import build_input, canonical, digest, file
from test_unit1_red import product


class InputGuards(unittest.TestCase):
    def admit(self, value, files):
        api=product('inputs');policy={'input_sha256':digest(value),'policy_sha256':'e'*64,'qualification_scope':'fixture'}
        return api.admit_build_input(api.parse_build_input(canonical(value)),files.__getitem__,policy)

    def test_base_image_provenance_is_measured(self):
        value,files=build_input()
        provenance=file('deps/base-provenance.json',b'known provenance')
        value['dependencies']['base_images']=[{'role':'runtime','reference':'example/image:1@sha256:'+'f'*64,
            'platform':'linux/amd64','manifest_digest':'f'*64,'config_digest':'b'*64,'provenance':provenance}]
        with self.assertRaises(ValueError):self.admit(value,files)

    def test_identifiers_have_utf8_byte_bound(self):
        api=product('inputs');value,files=build_input();value['recipe']['recipe_id']='é'*128
        with self.assertRaises(ValueError):api.parse_build_input(canonical(value))

    def test_recipe_must_be_literal_and_execute_admitted_entrypoint(self):
        for argv in (['sh','-c','echo public'],['bash','-lc','public'],['python3','foreign.py']):
            value,files=build_input();value['recipe']['argv_template']=argv
            with self.subTest(argv=argv),self.assertRaises(ValueError):self.admit(value,files)

    def test_compatibility_documents_are_decoded_and_bind_producer_identity(self):
        for field in ('compatibility_contract','schema_manifest','api_descriptor','event_index'):
            value,files=build_input();record=value['compatibility_inputs'][field]
            files[record['path']]=b'not-json\n';value['compatibility_inputs'][field]=file(record['path'],files[record['path']])
            with self.subTest(field=field),self.assertRaises(ValueError):self.admit(value,files)
        value,files=build_input();record=value['compatibility_inputs']['api_descriptor'];bad=json.loads(files[record['path']]);bad['module_id']='foreign'
        files[record['path']]=canonical(bad);value['compatibility_inputs']['api_descriptor']=file(record['path'],files[record['path']])
        with self.assertRaises(ValueError):self.admit(value,files)

    def test_wrong_descriptor_primitive_is_typed_refusal(self):
        value,files=build_input();path=value['descriptor']['path'];files[path]=canonical([]);value['descriptor']=file(path,files[path])
        try:self.admit(value,files);result='admitted'
        except ValueError:result='refused'
        except AttributeError:result='untyped-AttributeError'
        self.assertEqual(result,'refused')


if __name__=='__main__':unittest.main(verbosity=2)
