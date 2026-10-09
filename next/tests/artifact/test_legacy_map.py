"""Artifact associations must match their own frozen table body."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'scripts'))
from check_legacy_map import inspect_artifacts, parse_fields


class LegacyMap(unittest.TestCase):
    def fixture(self, source):
        digest=hashlib.sha256(source).hexdigest();fields=parse_fields(source.decode())
        mapping={'source_ref':'synthetic','source_file':'schema.sql','source_sha256':digest,'source_tables':len(fields),
                 'tables':[{'source_table':table,'source_columns':[{'name':name,'source_declaration':declaration} for name,declaration in columns]} for table,columns in fields.items()]}
        inventory={'source_ref':'synthetic','source':'schema.sql','sha256':digest,'tables':list(fields)}
        return mapping,inventory

    def test_with_options_do_not_extend_table_body(self):
        source="CREATE TABLE public.alpha (\n id integer, value text DEFAULT '),('::text\n)\nWITH (autovacuum_vacuum_scale_factor='0.02');\nCREATE TABLE public.beta (\n name text\n);"
        self.assertEqual(parse_fields(source),{'public.alpha':[('id','id integer'),('value',"value text DEFAULT '),('::text")],'public.beta':[('name','name text')]})

    def test_constraint_commas_and_quoted_comments_do_not_create_fields(self):
        source="CREATE TABLE public.alpha (\n id integer, note text DEFAULT '-- /* ( , ) */'::text,\n CONSTRAINT ok CHECK (id IN (1,2,3))\n);"
        self.assertEqual([name for name,_ in parse_fields(source)['public.alpha']],['id','note'])

    def test_neighbor_declaration_cannot_be_borrowed(self):
        source=b'CREATE TABLE public.alpha (\n id integer\n);\nCREATE TABLE public.beta (\n id text\n);'
        mapping,inventory=self.fixture(source)
        mapping['tables'][0]['source_columns'][0]['source_declaration']='id text'
        self.assertTrue(inspect_artifacts(source,mapping,inventory)['problems'])

    def test_duplicate_or_missing_columns_and_tables_fail(self):
        source=b'CREATE TABLE public.alpha (\n id integer\n);'
        for change in ('duplicate_column','missing_column','duplicate_table'):
            mapping,inventory=self.fixture(source)
            if change=='duplicate_column':mapping['tables'][0]['source_columns']*=2
            elif change=='missing_column':mapping['tables'][0]['source_columns']=[]
            else:mapping['tables']*=2
            with self.subTest(change=change):self.assertTrue(inspect_artifacts(source,mapping,inventory)['problems'])

    def test_actual_map_matches_pinned_source(self):
        source=Path(os.environ['LEGACY_SCHEMA_SOURCE']).read_bytes()
        evidence=Path(os.environ['LEGACY_MAP_DIRECTORY'])
        result=inspect_artifacts(source,json.loads((evidence/'legacy-schema-disposition.json').read_text()),json.loads((evidence/'legacy-schema-inventory.json').read_text()))
        self.assertEqual(result['problems'],[],result)
