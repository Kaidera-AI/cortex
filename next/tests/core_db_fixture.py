"""Reuse frozen C04 setup without inheriting its25 test methods."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent/'auth'))
import test_authorization as auth_fixture

READ_A=auth_fixture.READ_A
WRITE_A=auth_fixture.WRITE_A
OWNER_A=auth_fixture.OWNER_A
CONTROL_A=auth_fixture.CONTROL_A
READ_B=auth_fixture.READ_B
uid=auth_fixture.uid
API=auth_fixture.API
REQUEST=auth_fixture.REQUEST

class Fixture(unittest.TestCase):
    reset=auth_fixture.AuthorizationTests.reset
    auth=auth_fixture.AuthorizationTests.auth
    def setUp(self):auth_fixture.AuthorizationTests.setUp(self)
