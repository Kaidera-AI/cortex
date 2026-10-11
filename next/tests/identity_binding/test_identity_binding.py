"""Post-effect private binding loss must roll back before identity acceptance."""
from contextlib import contextmanager
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import UUID

from cortex_core.auth import AuthError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'auth_identity'))
import test_identity as frozen
from core_db_fixture import Fixture, uid


class IdentityBinding(Fixture):
    port = frozen.IdentityTests.port
    register = frozen.IdentityTests.register
    manifest = frozen.IdentityTests.manifest
    counts = frozen.IdentityTests.counts

    def test_private_binding_loss_after_actual_registration_refuses_and_rolls_back(self):
        port = self.port(); before = self.counts()
        original = frozen.identity_api.Identity._register
        def discard(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            self.assertEqual(self.request.execute('SELECT count(*) FROM auth.identity_lookup(%s)', (UUID(uid(70)),)).fetchone()[0], 1)
            self.request.execute('DISCARD TEMP')
            return result
        with patch.object(frozen.identity_api.Identity, '_register', discard), self.assertRaises(AuthError):
            self.register(port)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(port.lookup(UUID(uid(70))))

    def test_private_binding_loss_after_actual_adoption_refuses_and_rolls_back(self):
        port = self.port(donor=True); manifest = self.manifest()
        preview = port.dry_run_adoption(manifest); before = self.counts()
        original = frozen.identity_api.Identity._control
        @contextmanager
        def discard(instance):
            with original(instance) as scope:
                yield scope
                self.assertEqual(self.request.execute('SELECT count(*) FROM auth.identity_lookup(%s)', (UUID(uid(10)),)).fetchone()[0], 1)
                self.request.execute('DISCARD TEMP')
        with patch.object(frozen.identity_api.Identity, '_control', discard), self.assertRaises(AuthError):
            port.apply_adoption(manifest, approved_sha256=preview.manifest_sha256, request_key='binding-loss-adopt')
        self.assertEqual(self.counts(), before)
        self.assertIsNone(port.lookup(UUID(uid(10))))
