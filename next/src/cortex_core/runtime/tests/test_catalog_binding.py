"""Accessor ownership controls; fixed-module doubles never prove SQL policy."""
from contextlib import contextmanager
import json
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.runtime import resources
from cortex_core.runtime.tests import test_resources as fixtures


class CatalogBindingTests(unittest.TestCase):
    setUp=fixtures.ResourceTests.setUp
    vault=fixtures.ResourceTests.vault
    data=fixtures.CatalogTests.data
    driver=fixtures.CatalogTests.driver
    opened=fixtures.CatalogTests.opened

    def require_source(self):
        self.assertTrue(hasattr(resources.SeedResources,'catalog'),
            'actual sole-validator SeedResources.catalog accessor is missing')

    @contextmanager
    def policy(self):
        self.validations=[];self.ledgers=[]
        module=ModuleType('cortex_ship.install.roles')
        def validate(config,observation,*,phase):
            self.validations.append((config,observation,phase))
            return json.loads(json.dumps(dict(admitted=observation,phase=phase)))
        def schema(connection):self.ledgers.append(connection);return True
        module.validate_catalog=validate
        with patch.dict(sys.modules,{'cortex_ship.install.roles':module}), \
             patch('cortex_core.schema.readiness.check_schema_ready',side_effect=schema):yield module

    def test_actual_owner_schema_snapshot_and_fixed_validator_are_one_closed_path(self):
        self.require_source()
        with self.opened() as owned,self.policy():
            result=owned.catalog()
            self.assertEqual(self.ledgers,[owned.bootstrap]);self.assertEqual(len(self.validations),1)
            config,observed,phase=self.validations[0]
            self.assertIs(config,self.config);self.assertEqual(phase,'seed')
            self.assertEqual(result,dict(admitted=observed,phase='seed'))
            self.assertEqual(observed['session']['login'],'actual-session')
            self.assertEqual(self.events.count('begin:cortex_bootstrap'),1)
            self.assertEqual(self.events.count('commit:cortex_bootstrap'),1)
            self.assertTrue(any('REPEATABLE READ READ ONLY' in sql for sql,p in owned.bootstrap.queries))
            self.assertFalse(any(connection.queries for connection in self.calls[1:]))

    def test_full_schema_failure_rolls_back_before_policy_and_never_admits(self):
        self.require_source()
        with self.opened() as owned,self.policy(), \
             patch('cortex_core.schema.readiness.check_schema_ready',side_effect=RuntimeError('private-password')):
            with self.assertRaisesRegex(ValueError,'catalog_unavailable') as error:owned.catalog()
            self.assertNotIn('private-password',str(error.exception));self.assertEqual(self.validations,[])
            self.assertIn('rollback:cortex_bootstrap',self.events)
            self.assertFalse(any('FROM pg_catalog.pg_database' in sql for sql,p in owned.bootstrap.queries))

    def test_missing_fixed_module_or_callable_refuses_without_old_fallback(self):
        self.require_source()
        for replacement in (None,ModuleType('empty')):
            with self.subTest(replacement=replacement),self.opened() as owned,self.policy(), \
                 patch.dict(sys.modules,{'cortex_ship.install.roles':replacement}):
                with self.assertRaisesRegex(ValueError,'catalog_validator_unavailable'):owned.catalog()
                self.assertEqual(self.validations,[]);self.assertEqual(self.ledgers,[])

    def test_policy_failure_or_non_dict_result_has_no_public_authority_or_details(self):
        self.require_source()
        for effect in ('raise','non-dict'):
            with self.subTest(effect=effect),self.opened() as owned,self.policy() as module:
                def bad(*args,**kw):
                    if effect=='raise':raise RuntimeError('private-password')
                    return True
                module.validate_catalog=bad
                with self.assertRaisesRegex(ValueError,'catalog_refused') as error:owned.catalog()
                self.assertNotIn('private-password',str(error.exception))

    def test_each_call_observes_fresh_and_preserves_actual_operation_data(self):
        self.require_source()
        with self.opened() as owned,self.policy():
            first=owned.catalog();first['admitted']['bindings'][0]['operation_id']='changed'
            second=owned.catalog()
            self.assertEqual(second['admitted']['bindings'][0]['operation_id'],
                '10000000-0000-0000-0000-000000000010')
            self.assertEqual(self.events.count('begin:cortex_bootstrap'),2)
            self.assertEqual(len(self.validations),2)

    def test_original_deadline_is_checked_after_the_sole_policy_returns(self):
        self.require_source()
        with self.opened() as owned,self.policy() as module:
            original=module.validate_catalog
            def expires(*args,**kw):
                result=original(*args,**kw)
                object.__setattr__(owned,'_deadline',time.monotonic()-1)
                return result
            module.validate_catalog=expires
            with self.assertRaisesRegex(ValueError,'resources_deadline'):owned.catalog()

    def test_closed_unbound_or_caller_supplied_snapshot_never_reaches_policy(self):
        self.require_source()
        from cortex_core.runtime.catalog import validated_seed
        with self.opened() as owned,self.policy():
            with self.assertRaises(TypeError):owned.catalog(self.data())
            with self.assertRaisesRegex(ValueError,'resources_unbound'):validated_seed(SimpleNamespace())
            with owned.transaction('bootstrap'):
                with self.assertRaisesRegex(ValueError,'resources_transaction'):owned.catalog()
            self.assertEqual(self.validations,[])
        with self.assertRaisesRegex(ValueError,'resources_unbound'):owned.catalog()


if __name__=='__main__':unittest.main()
