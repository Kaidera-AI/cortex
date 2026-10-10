"""The standalone migrator must import the API from its script-path entry."""

from pathlib import Path
import unittest

import yaml


COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


class MigratorPythonPath(unittest.TestCase):
    def test_migrator_inherits_api_env_and_imports_app(self):
        compose = yaml.safe_load(COMPOSE.read_text())
        common_env = compose["x-api-env"]
        migrate_env = compose["services"]["cortex-migrate"]["environment"]

        self.assertEqual(common_env, {key: migrate_env.get(key) for key in common_env})
        self.assertIn("/app", migrate_env.get("PYTHONPATH", "").split(":"))


if __name__ == "__main__":
    unittest.main()
