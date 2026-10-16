"""X2 Linux owner-binding fixture contract; no host or native qualification."""
import sys
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.install.platforms import linux
from test_gate_b_linux import LinuxObserverOracle


class X2LinuxObserverBinding(unittest.TestCase):
    def test_fixed_owner_supplier_delegates_to_linux_zero_argument_provider(self):
        expected = LinuxObserverOracle().inputs("positive")
        with patch.object(sys, "platform", "linux"), \
             patch.object(linux, "_installed_linux_inputs", return_value=expected) as provider:
            try:
                result = owner._installed_owner_inputs()
            except owner.OwnerRefusal as error:
                self.fail("X2 Linux provider was not reached: " + error.detail)
        self.assertIs(type(result), owner._Inputs)
        self.assertIs(result, expected)
        provider.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
