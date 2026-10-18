"""S3 catalog compatibility ONLY: roles.validate_catalog admits an `install` config for phase='seed' solely with an actual live Core SEED resource context
(`live=` the actual current SeedResources for the SAME config/vault/deadline), and keeps every existing predicate and the ordinary seed/serve denials.
The install CONFIG here is the REAL Core1 decoder (read_config_file(mode='install')). FAKE BOUNDARIES (named): SeedResourcesDouble replaces `cortex_core.runtime.resources.SeedResources`
and `current_seed_resources` (Core's real resources are private and need a database); catalog.validated_seed is run for real with a SeedResourcesDouble and an injected
`cortex_ship.install.roles` module that records the call. NOT RUN: Core2's real SEED stage, a real SeedResources, any PostgreSQL."""
import sys
import types
import unittest
from unittest import mock

import test_catalog as TC
import roles as R
from cortex_core.runtime import catalog as CATALOG, resources as RESOURCES
from cortex_core.runtime.bootstrap_config import read_config_file


class Refusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SeedResourcesDouble:
    """NAMED DOUBLE of Core's SeedResources: carries the config/deadline the real class exposes to catalog.py."""

    def __init__(self, config, deadline=1234.5):
        self._config, self._deadline, self._in_transaction = config, deadline, False

    def check_deadline(self):
        return None


def current_seed_resources_double(expected):
    def current(config, deadline):
        if config is not expected._config or deadline != expected._deadline:
            raise Refusal('resources_unbound')
        return expected
    return current


class InstallCatalog(TC.Base):
    def setUp(self):
        super().setUp()
        self.install = read_config_file(self.vault / 'config/cortex.json', mode='install')
        self.live = SeedResourcesDouble(self.install)
        patches = [mock.patch.object(RESOURCES, 'SeedResources', SeedResourcesDouble),
                   mock.patch.object(RESOURCES, 'current_seed_resources', current_seed_resources_double(self.live))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def outcome(self, config, obs, **kw):
        try:
            return ('ok', R.validate_catalog(config, obs, **kw))
        except R.RoleRefusal as error:
            return ('refused', error.reason)
        except TypeError as error:
            return ('TypeError', str(error)[:60])

    def test_an_install_config_is_admitted_for_seed_only_with_the_actual_live_seed_resources(self):
        ordinary = R.validate_catalog(self.cfg, self.healthy())
        outcome = self.outcome(self.install, self.healthy(), phase='seed', live=self.live)
        self.assertEqual(outcome[0], 'ok')
        self.assertEqual(sorted(outcome[1]), sorted(ordinary))

    def test_an_install_config_without_live_resources_is_refused_before_policy(self):
        self.assertEqual(self.outcome(self.install, self.healthy(), phase='seed'), ('refused', 'catalog_live'))
        self.assertEqual(self.outcome(self.install, {}, phase='seed'), ('refused', 'catalog_live'), 'refused before any observation field is read')

    def test_live_resources_of_another_config_or_a_wrong_type_or_a_refusing_core_check_do_not_admit(self):
        other = SeedResourcesDouble(read_config_file(self.vault / 'config/cortex.json', mode='install'))
        self.assertEqual(self.outcome(self.install, self.healthy(), phase='seed', live=other), ('refused', 'catalog_live'))
        for bad in (object(), {'_config': self.install}, None, True):
            self.assertEqual(self.outcome(self.install, self.healthy(), phase='seed', live=bad), ('refused', 'catalog_live'), repr(bad))
        with mock.patch.object(RESOURCES, 'current_seed_resources', side_effect=Refusal('resources_unbound')):
            self.assertEqual(self.outcome(self.install, self.healthy(), phase='seed', live=self.live), ('refused', 'catalog_live'))

    def test_an_install_config_is_never_admitted_for_serve_or_by_a_live_argument_alone(self):
        self.assertEqual(self.outcome(self.install, self.healthy(), phase='serve', live=self.live), ('refused', 'catalog_config'))
        self.assertEqual(self.outcome(self.cfg, self.healthy(), phase='serve', live=self.live)[0], 'refused', 'the serve view stays the serve view')

    def test_every_existing_predicate_still_applies_to_an_install_config(self):
        obs = self.healthy()
        obs['session']['login'] = TC.LOGINS['migrator']
        ordinary = self.outcome(self.cfg, obs)
        self.assertEqual(ordinary, ('refused', 'catalog_session'))
        self.assertEqual(self.outcome(self.install, obs, phase='seed', live=self.live), ordinary)

    def test_the_ordinary_seed_view_is_unchanged_and_needs_no_live_argument(self):
        self.assertEqual(self.outcome(self.cfg, self.healthy())[0], 'ok')
        cfg = self.cfg
        self.assertEqual(self.outcome(cfg, self.healthy(), phase='seed')[0], 'ok')


class ValidatedSeedCaller(unittest.TestCase):
    """Core's catalog.validated_seed (real code) passes the live resources for an install config and nothing extra for the ordinary seed view."""

    def run_validated(self, config):
        calls = []

        def validate(cfg, observation, **kw):
            calls.append(kw)
            return {'ok': True}

        module = types.ModuleType('cortex_ship.install.roles')
        module.validate_catalog = validate
        resources = SeedResourcesDouble(config)
        patches = [mock.patch.dict(sys.modules, {'cortex_ship': types.ModuleType('cortex_ship'), 'cortex_ship.install': types.ModuleType('cortex_ship.install'), 'cortex_ship.install.roles': module}),
                   mock.patch.object(RESOURCES, 'SeedResources', SeedResourcesDouble),
                   mock.patch.object(RESOURCES, 'current_seed_resources', current_seed_resources_double(resources)),
                   mock.patch.object(CATALOG, '_observe', return_value={'synthetic': 'observation'})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        try:
            return CATALOG.validated_seed(resources), calls, resources
        except Exception as error:
            return ('exc', getattr(error, 'reason', str(error))), calls, resources

    def test_an_install_config_hands_the_validator_the_actual_live_resources(self):
        cfg = types.SimpleNamespace(mode='install')
        result, calls, resources = self.run_validated(cfg)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get('live'), resources)
        self.assertEqual(calls[0].get('phase'), 'seed')

    def test_the_ordinary_seed_view_calls_the_validator_exactly_as_before(self):
        cfg = types.SimpleNamespace(mode='seed')
        result, calls, resources = self.run_validated(cfg)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(calls, [{'phase': 'seed'}])


if __name__ == '__main__':
    unittest.main()
