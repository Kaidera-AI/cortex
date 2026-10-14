"""Versioned keyword variant alongside unchanged semantic/C02 fixtures."""
import copy
import json
from pathlib import Path
import unittest

from cortex_core.openkai_contract import validate_case,ContractRefusal

ROOT=Path(__file__).resolve().parents[2]


class KeywordConsumer(unittest.TestCase):
    def test_keyword_variant_and_status_corruption_are_deciding(self):
        packet=json.loads((ROOT/'contracts/fixtures/openkai/keyword-search-v1.json').read_text())
        self.assertEqual(packet['owner_mapping_sha256'],'dd54d5354eb9b1447f41337ef40e334e01536b60bb0040a0dc121818bb17c352')
        for case in packet['cases']:
            with self.subTest(case=case['id']):
                try:actual=validate_case(case)
                except ContractRefusal as error:self.fail('keyword variant rejected: '+str(error))
                self.assertEqual(actual,case['id'])
                bad=copy.deepcopy(case);bad['status']=200 if case['status']==503 else 503
                with self.assertRaises(ContractRefusal):validate_case(bad)

    def test_legacy_semantic_partial_fixture_remains_distinct(self):
        old=json.loads((ROOT/'contracts/fixtures/openkai/search-states.json').read_text())
        partial=next(c for c in old['cases'] if c['state']=='partial')
        self.assertEqual(validate_case(partial),partial['id'])
        self.assertEqual(partial['wire_state'],'ruled_unimplemented')
        self.assertNotIn('results',partial)
