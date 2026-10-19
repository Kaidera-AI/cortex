"""J1 additional source boundary controls; no real IO, key or SQL."""
from contextlib import contextmanager
import copy
import unittest
from unittest.mock import patch

from cortex_core.runtime import custody
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests import test_project_custody as fixtures


class ProjectBoundaryTests(unittest.TestCase):
    setUp = fixtures.ProjectCustodyTests.setUp
    config = fixtures.ProjectCustodyTests.config
    opened = fixtures.ProjectCustodyTests.opened
    intent = fixtures.ProjectCustodyTests.intent
    receipt = fixtures.ProjectCustodyTests.receipt
    prepare = fixtures.ProjectCustodyTests.prepare

    def test_caught_write_failure_taints_owner_before_next_subject(self):
        with self.opened() as owner:
            with owner.record('project_worker', subject_id=fixtures.SUBJECT) as record:
                self.adapters.write_error = Refusal('synthetic_write')
                with self.assertRaises(Refusal):
                    record.create(intent=self.intent(), credential=fixtures.PREIMAGE)
                self.adapters.write_error = None
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                with owner.record('project_worker', subject_id=fixtures.OTHER):
                    self.fail('caught IO failure must taint the original owner')
            self.assertEqual(self.adapters.events, before)

    def test_absent_read_expiry_cannot_publish_empty_reconciliation(self):
        with self.opened() as owner:
            with owner.record('project_worker', subject_id=fixtures.SUBJECT) as record:
                def read(directory, name):
                    result = self.adapters.read(directory, name)
                    self.adapters.clock = 200.0
                    return result
                with patch.object(custody.custody_fs, 'read_record', read):
                    with self.assertRaises(Refusal): record.load()

    def test_committed_replay_expiry_cannot_publish_cached_material(self):
        with self.opened() as owner:
            intent, _ = self.prepare(owner)
            with owner.record('project_worker', subject_id=fixtures.SUBJECT) as record:
                record.load(); record.commit(receipt=self.receipt(intent))
                def read(directory, name):
                    result = self.adapters.read(directory, name)
                    self.adapters.clock = 200.0
                    return result
                with patch.object(custody.custody_fs, 'read_record', read):
                    with self.assertRaises(Refusal): record.commit(receipt=self.receipt(intent))

    def test_foreign_reference_copy_and_lock_cleanup_refuse(self):
        with self.opened() as owner: self.prepare(owner)
        name = 'project_worker_' + fixtures.SUBJECT
        other = 'project_worker_' + fixtures.OTHER
        root = self.value['vault_root']
        self.adapters.records[root, other] = self.adapters.records[root, name]
        with self.opened() as owner:
            with self.assertRaises(Refusal):
                with owner.record('project_worker', subject_id=fixtures.OTHER) as record: record.load()
        lock = self.adapters.lock
        @contextmanager
        def broken_cleanup(directory, name):
            with lock(directory, name): yield
            raise Refusal('synthetic_cleanup')
        with self.opened() as owner:
            with patch.object(custody.custody_fs, 'record_lock', broken_cleanup):
                with self.assertRaises(Refusal):
                    with owner.record('project_worker', subject_id=fixtures.SUBJECT): pass
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                with owner.record('project_worker', subject_id=fixtures.OTHER): self.fail('tainted owner')
            self.assertEqual(self.adapters.events, before)

    def test_key_stays_outside_vault_and_original_deadline_is_finite(self):
        self.config()
        value = copy.deepcopy(self.value)
        value['custody_unlock']['path'] = self.value['vault_root'] + '/NONSECRET-project.key'
        with self.assertRaises(Refusal):
            custody_config = fixtures.decode_config(fixtures.json.dumps(value).encode(), mode='project')
        for deadline in (True, 100.0, 0, float('inf'), float('nan'), 10**1000):
            with self.subTest(deadline=deadline), self.assertRaises(Refusal):
                with self.opened(deadline=deadline): self.fail('invalid original deadline')
        self.assertEqual(self.adapters.events, [])


if __name__ == '__main__': unittest.main()
