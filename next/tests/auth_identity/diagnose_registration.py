"""Disposable-only SQL diagnostic; no plaintext or request arguments printed."""
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_identity
import psycopg

test = test_identity.IdentityTests('test_owner_registers_stable_agent_roles_separate_permissions_and_digest_only')
test.setUp()
port = test.port()
original = port._register
def traced(*args, **kwargs):
    try:
        return original(*args, **kwargs)
    except psycopg.Error as error:
        message = re.sub(r'[0-9a-f]{64}', '<digest>', error.diag.message_primary or '')
        print(json.dumps({'type': type(error).__name__, 'sqlstate': error.sqlstate, 'primary': message}))
        raise
port._register = traced
try:
    test.register(port)
except test_identity.AuthError as error:
    print(json.dumps({'private_refusal': error.code}))
finally:
    test.doCleanups()
