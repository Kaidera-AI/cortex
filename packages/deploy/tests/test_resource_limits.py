"""DEPLOY-MEM-001: every shipped Compose service has bounded resources."""

import os
from pathlib import Path
import re
import unittest

import yaml

from image_manifest import SERVICE_ROLES


COMPOSE = Path(__file__).resolve().parents[1] / 'docker-compose.yml'
MEMORY = re.compile(r'[1-9][0-9]*(?:[kKmMgG])?')


class ResourceLimitContract(unittest.TestCase):
    def test_all_product_services_have_memory_and_cpu_limits(self):
        source = Path(os.environ.get('CORTEX_RESOURCE_TEST_COMPOSE', COMPOSE))
        services = yaml.safe_load(source.read_text())['services']
        self.assertEqual(set(services), set(SERVICE_ROLES))
        missing = []
        for name, config in services.items():
            memory = config.get('mem_limit')
            cpu = config.get('cpus')
            if not isinstance(memory, str) or not MEMORY.fullmatch(memory):
                missing.append((name, 'memory'))
            if type(cpu) not in (int, float) or cpu <= 0:
                missing.append((name, 'cpu'))
        self.assertEqual(missing, [], 'every product service needs explicit memory and CPU limits')


if __name__ == '__main__':
    unittest.main()
