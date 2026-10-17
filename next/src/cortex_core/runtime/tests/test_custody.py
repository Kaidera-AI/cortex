"""J06 source controls. Missing implementation fails in the deciding test BODY."""
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.tests.test_bootstrap_config import sample
try:
    from cortex_core.runtime import custody, custody_fs
except ImportError:
    custody = custody_fs = None

KEY = bytes(range(32))  # public test fixture, never an operator key
SECRET = b'c' * 64     # public test fixture, never an enrolled credential


def intent(value):
    return dict(operation_key='test-operation', installation_id=value['installation_id'],
                tenant_id=value['tenant_id'], project_id=value['project_id'],
                principal_id='10000000-0000-0000-0000-000000000004',
                credential_id='10000000-0000-0000-0000-000000000005',
                client_ref=value['client']['credential_ref'], schema_prefix_sha256='a'*64,
                credential_sha256=hashlib.sha256(SECRET).hexdigest(),
                database={'name':'cortex','oid':42,'owner_oid':43},
                roles={name:dict(login=role['login'],oid=100+i,revision=1,
                    flags=dict(login=True,superuser=False,createdb=False,createrole=False,
                               inherit=False,replication=False,bypassrls=False),memberships=[])
                       for i,(name,role) in enumerate(value['roles'].items())})


def receipt(value):
    from cortex_core.release.codec import canonical_bytes
    return {**{k:value[k] for k in ('installation_id','tenant_id','project_id','principal_id',
                                  'credential_id','operation_key','credential_sha256')},
            'intent_sha256':hashlib.sha256(canonical_bytes(value)).hexdigest()}


class SnapshotIdentity(unittest.TestCase):
    def test_same_config_all_modes_has_same_complete_snapshot(self):
        value=sample('/private/test-vault')
        views=[decode_config(json.dumps(value).encode(),mode=m) for m in ('seed','serve','migrate','client','status')]
        self.assertTrue(hasattr(views[0],'source_sha256'),'actual Core carrier needs full snapshot identity')
        self.assertEqual(len({c.source_sha256 for c in views}),1)
        self.assertTrue(all(c.client_ref==value['client']['credential_ref'] for c in views))
        self.assertTrue(all(dict(c.role_logins)=={n:r['login'] for n,r in value['roles'].items()} for c in views))
        with self.assertRaises(TypeError):views[0].role_logins['migrator']='other'
        self.assertEqual(set(views[1].roles),{'api_request','probe'})

    def test_full_snapshot_changes_for_hidden_other_mode_role_and_semantic_bytes(self):
        value=sample('/private/test-vault');original=decode_config(json.dumps(value).encode(),mode='serve')
        self.assertTrue(hasattr(original,'source_sha256'))
        changed=copy.deepcopy(value);changed['roles']['migrator']['secret_ref']='secrets/cortex/other'
        alternate=decode_config(json.dumps(changed).encode(),mode='serve')
        self.assertNotEqual(original.source_sha256,alternate.source_sha256)
        pretty=decode_config(json.dumps(value,indent=2).encode(),mode='serve')
        self.assertEqual(original.source_sha256,pretty.source_sha256)


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        (self.root/'secrets').mkdir(mode=0o700);(self.root/'secrets/cortex').mkdir(mode=0o700)
        self.value=sample(self.root);self.expected=intent(self.value);self.ack=receipt(self.expected)
        self.leaf=self.root/'secrets/cortex/client'

    def config(self,mode='seed',value=None):
        return decode_config(json.dumps(value or self.value).encode(),mode=mode)

    @contextmanager
    def opened(self,mode='seed',*,key=KEY,key_id='test-key',value=None):
        r,w=os.pipe()
        try:
            os.write(w,key);os.close(w);w=None
            with custody.open_custody(self.config(mode,value),unlock_fd=r,key_id=key_id) as vault:
                yield vault
        finally:
            os.close(r)
            if w is not None:os.close(w)

    def stage(self):
        with self.opened() as vault:
            with vault.seed_record() as record:
                return record.create(intent=self.expected,credential=SECRET)

    def commit(self):
        with self.opened() as vault:
            with vault.seed_record() as record:
                record.load();return record.commit(receipt=self.ack)

    def test_ciphertext_before_return_exact_replay_and_committed_client(self):
        self.assertIsNotNone(custody)
        material=self.stage();raw=self.leaf.read_bytes()
        self.assertNotIn(SECRET,raw);self.assertNotIn(base64.b64encode(SECRET),raw)
        self.assertEqual(material.state,'prepared');self.assertEqual(material.generation,0)
        self.assertNotIn(SECRET.decode(),repr(material))
        with self.opened('client') as vault:
            with self.assertRaisesRegex(ValueError,'custody_uncommitted'):vault.resolve('client')
        self.commit()
        with self.opened() as vault:
            with vault.seed_record() as record:
                saved=record.load();self.assertEqual(saved.credential,SECRET)
                self.assertEqual(saved.intent,self.expected);self.assertEqual(saved.receipt,self.ack)
                self.assertEqual(saved.state,'committed');self.assertEqual(saved.generation,1)
                before=self.leaf.read_bytes();record.commit(receipt=self.ack)
                self.assertEqual(before,self.leaf.read_bytes())
        with self.opened('client') as vault:self.assertEqual(vault.resolve('client'),SECRET)

    def test_absence_only_missing_file_not_corruption(self):
        self.assertIsNotNone(custody)
        with self.opened() as vault:
            with vault.seed_record() as record:self.assertIsNone(record.load())
        self.leaf.write_bytes(b'not-encrypted');self.leaf.chmod(0o600)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()

    def test_material_has_defensive_copy_and_no_public_mutation(self):
        self.assertIsNotNone(custody)
        material=self.stage();value=material.intent;value['database']['oid']=999
        self.assertEqual(material.intent['database']['oid'],42)
        with self.assertRaises((AttributeError,TypeError)):material.state='committed'
        self.commit()
        with self.opened() as vault:
            with vault.seed_record() as record:
                m=record.load();ack=m.receipt;ack['principal_id']='changed'
                self.assertEqual(m.receipt,self.ack)

    def test_create_is_absent_only_and_commit_needs_loaded_record(self):
        self.assertIsNotNone(custody)
        self.stage()
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaises(ValueError):record.create(intent=self.expected,credential=SECRET)
                with self.assertRaises(ValueError):record.commit(receipt=self.ack)
                record.load();record.commit(receipt=self.ack)
                with self.assertRaises(ValueError):record.commit(receipt={**self.ack,'operation_key':'other'})

    def test_seed_client_and_role_modes_refuse_before_file_io(self):
        self.assertIsNotNone(custody)
        with self.opened('serve') as vault:
            with patch.object(custody_fs,'read_record',side_effect=AssertionError('filesystem reached')):
                for purpose in ('bootstrap','migrator','installer_enrollment','client'):
                    with self.subTest(purpose=purpose),self.assertRaisesRegex(ValueError,'purpose_not_admitted'):
                        vault.resolve(purpose)
                with self.assertRaisesRegex(ValueError,'purpose_not_admitted'):vault.seed_record().__enter__()
        with self.opened('client') as vault:
            with self.assertRaisesRegex(ValueError,'purpose_not_admitted'):vault.import_credential('api_request',b'not-admitted')

    def test_import_role_exact_replay_and_no_plaintext_fallback(self):
        self.assertIsNotNone(custody)
        with self.opened('serve') as vault:
            vault.import_credential('api_request',b'public-test-database-password')
            self.assertEqual(vault.resolve('api_request'),b'public-test-database-password')
            raw=(self.leaf.parent/'api_request').read_bytes()
            self.assertNotIn(b'public-test-database-password',raw)
            vault.import_credential('api_request',b'public-test-database-password')
            with self.assertRaises(ValueError):vault.import_credential('api_request',b'changed-password')
        path=self.leaf.parent/'probe';path.write_bytes(b'plaintext-is-not-authority');path.chmod(0o600)
        with self.opened('serve') as vault:
            with self.assertRaisesRegex(ValueError,'custody_invalid'):vault.resolve('probe')

    def test_wrong_key_key_id_or_tag_closed_refusal(self):
        self.assertIsNotNone(custody)
        self.stage()
        for key,key_id in [(b'x'*32,'test-key'),(KEY,'different-key')]:
            with self.subTest(key_id=key_id),self.opened(key=key,key_id=key_id) as vault:
                with vault.seed_record() as record:
                    with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()
        d=json.loads(self.leaf.read_bytes());raw=bytearray(base64.b64decode(d['ciphertext']));raw[-1]^=1
        d['ciphertext']=base64.b64encode(raw).decode();self.leaf.write_text(json.dumps(d))
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()

    def test_aad_binds_project_reference_purpose_and_generation(self):
        self.assertIsNotNone(custody)
        self.stage();raw=self.leaf.read_bytes()
        changed=copy.deepcopy(self.value);changed['project_id']='20000000-0000-0000-0000-000000000003';changed['project_names']={'sample':changed['project_id']}
        with self.opened(value=changed) as vault:
            with vault.seed_record() as record:
                with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()
        changed=copy.deepcopy(self.value);changed['client']['credential_ref']='secrets/cortex/other'
        other=self.leaf.parent/'other';other.write_bytes(raw);other.chmod(0o600)
        with self.opened(value=changed) as vault:
            with vault.seed_record() as record:
                with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()
        role=self.leaf.parent/'api_request';role.write_bytes(raw);role.chmod(0o600)
        with self.opened('serve') as vault:
            with self.assertRaisesRegex(ValueError,'custody_invalid'):vault.resolve('api_request')
        d=json.loads(raw);d['generation']+=1;self.leaf.write_text(json.dumps(d))
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaisesRegex(ValueError,'custody_invalid'):record.load()

    def test_ciphertext_envelope_is_closed_bounded_and_duplicate_free(self):
        self.assertIsNotNone(custody)
        self.stage();original=self.leaf.read_bytes();d=json.loads(original)
        variants=[original.replace(b'{',b'{"schema":"duplicate",',1),json.dumps({**d,'extra':1}).encode(),
                  json.dumps({**d,'generation':True}).encode(),json.dumps({**d,'nonce':'!'}).encode(),b'x'*65537,b'']
        for raw in variants:
            with self.subTest(length=len(raw)):
                self.leaf.write_bytes(raw)
                with self.opened() as vault:
                    with vault.seed_record() as record:
                        with self.assertRaises(ValueError):record.load()

    def test_unsafe_leaf_link_parent_mode_and_device_refuse(self):
        self.assertIsNotNone(custody)
        self.stage();self.leaf.chmod(0o644)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaises(ValueError):record.load()
        self.leaf.chmod(0o600);alias=self.leaf.parent/'alias';os.link(self.leaf,alias)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaises(ValueError):record.load()
        alias.unlink();self.leaf.rename(alias);self.leaf.symlink_to(alias)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaises(ValueError):record.load()
        self.leaf.unlink();os.mkfifo(self.leaf,0o600)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with self.assertRaises(ValueError):record.load()

    def test_unsafe_root_parent_and_lock_refuse(self):
        self.assertIsNotNone(custody)
        for where in (self.root,self.root/'secrets',self.leaf.parent):
            where.chmod(0o755)
            with self.subTest(where=where.name),self.assertRaises(ValueError):
                with self.opened():pass
            where.chmod(0o700)
        (self.root/'secrets').rename(self.root/'old');(self.root/'secrets').symlink_to(self.root/'old',target_is_directory=True)
        with self.assertRaises(ValueError):
            with self.opened():pass
        (self.root/'secrets').unlink();(self.root/'old').rename(self.root/'secrets')
        (self.leaf.parent/'.locks').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):
            with self.opened() as vault:
                with vault.seed_record():pass

    def test_lock_busy_and_crash_release_are_not_acceptance_markers(self):
        self.assertIsNotNone(custody)
        with self.opened() as first,self.opened() as second:
            with first.seed_record() as a:
                with self.assertRaisesRegex(ValueError,'custody_busy'):
                    with second.seed_record():pass
                a.create(intent=self.expected,credential=SECRET)
            with second.seed_record() as b:self.assertEqual(b.load().state,'prepared')
        try:
            with self.opened() as vault:
                with vault.seed_record():raise RuntimeError('simulated caller crash')
        except RuntimeError:pass
        with self.opened() as vault:
            with vault.seed_record() as record:self.assertEqual(record.load().credential,SECRET)

    def test_changed_leaf_during_transaction_refuses_without_overwrite(self):
        self.assertIsNotNone(custody)
        self.stage()
        with self.opened() as vault:
            with vault.seed_record() as record:
                record.load();saved=self.leaf.read_bytes();other=self.leaf.parent/'replacement'
                other.write_bytes(saved);other.chmod(0o600);os.replace(other,self.leaf)
                with self.assertRaisesRegex(ValueError,'custody_changed'):record.commit(receipt=self.ack)
                self.assertEqual(self.leaf.read_bytes(),saved)

    def test_closed_context_and_released_record_refuse(self):
        self.assertIsNotNone(custody)
        with self.opened() as vault:
            with vault.seed_record() as record:record.create(intent=self.expected,credential=SECRET)
            with self.assertRaises(ValueError):record.load()
        with self.assertRaises(ValueError):vault.resolve('api_request')
        with self.assertRaises(ValueError):
            with vault.seed_record():pass

    def test_failed_file_fsync_never_publishes_prepared(self):
        self.assertIsNotNone(custody)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with patch.object(custody_fs.os,'fsync',side_effect=OSError('private-ERROR-SENTINEL')):
                    with self.assertRaises(ValueError) as caught:record.create(intent=self.expected,credential=SECRET)
                self.assertNotIn('SENTINEL',str(caught.exception));self.assertFalse(self.leaf.exists())

    def test_ambiguous_directory_fsync_refuses_but_replay_keeps_secret(self):
        self.assertIsNotNone(custody)
        original=os.fsync;count=0
        def fault(fd):
            nonlocal count
            count+=1
            if count==2:raise OSError('directory fsync acknowledgement lost')
            return original(fd)
        with self.opened() as vault:
            with vault.seed_record() as record:
                with patch.object(custody_fs.os,'fsync',side_effect=fault):
                    with self.assertRaisesRegex(ValueError,'custody_commit_unknown'):record.create(intent=self.expected,credential=SECRET)
        with self.opened() as vault:
            with vault.seed_record() as record:self.assertEqual(record.load().credential,SECRET)

    def test_failed_checkpoint_preserves_prepared_not_false_commit(self):
        self.assertIsNotNone(custody)
        self.stage();before=self.leaf.read_bytes()
        with self.opened() as vault:
            with vault.seed_record() as record:
                record.load()
                with patch.object(custody_fs.os,'replace',side_effect=OSError('replace failed')):
                    with self.assertRaises(ValueError):record.commit(receipt=self.ack)
        self.assertEqual(self.leaf.read_bytes(),before)
        with self.opened('client') as vault:
            with self.assertRaisesRegex(ValueError,'custody_uncommitted'):vault.resolve('client')

    def test_new_nonce_every_actual_rewrite_and_private_leaf_modes(self):
        self.assertIsNotNone(custody)
        self.stage();first=json.loads(self.leaf.read_bytes());self.commit();second=json.loads(self.leaf.read_bytes())
        self.assertNotEqual(first['nonce'],second['nonce']);self.assertEqual(self.leaf.stat().st_mode&0o777,0o600)
        self.assertEqual(self.leaf.stat().st_nlink,1)
        self.assertEqual(set(second),{'schema','key_id','generation','nonce','ciphertext'})
        self.assertEqual(second['schema'],'cortex.custody.v1')

    def test_bad_unlock_size_type_and_key_identifier_refuse(self):
        self.assertIsNotNone(custody)
        for raw in (b'',b'x'*31,b'x'*33):
            with self.subTest(size=len(raw)),self.assertRaises(ValueError):
                with self.opened(key=raw):pass
        with self.assertRaises(ValueError):
            with self.opened(key_id='../bad'):pass
        for fd in (True,-1,1,2):
            with self.subTest(fd=fd),self.assertRaises(ValueError):
                with custody.open_custody(self.config(),unlock_fd=fd,key_id='test-key'):pass

    def test_regular_unlock_is_explicit_private_read_only_and_caller_owned(self):
        self.assertIsNotNone(custody)
        keyfile=self.root/'test-key-input';keyfile.write_bytes(KEY);keyfile.chmod(0o600)
        fd=os.open(keyfile,os.O_RDONLY)
        try:
            with custody.open_custody(self.config(),unlock_fd=fd,key_id='test-key'):pass
            self.assertEqual(os.fstat(fd).st_size,32)
        finally:os.close(fd)
        for mode,flags in [(0o644,os.O_RDONLY),(0o600,os.O_RDWR)]:
            keyfile.chmod(mode);fd=os.open(keyfile,flags)
            try:
                with self.subTest(mode=mode,flags=flags),self.assertRaises(ValueError):
                    with custody.open_custody(self.config(),unlock_fd=fd,key_id='test-key'):pass
            finally:os.close(fd)

    def test_unlock_pipe_missing_eof_is_bounded_and_sanitized(self):
        self.assertIsNotNone(custody)
        r,w=os.pipe();os.write(w,KEY);started=time.monotonic()
        try:
            with self.assertRaisesRegex(ValueError,'custody_locked'):
                with custody.open_custody(self.config(),unlock_fd=r,key_id='test-key'):pass
            self.assertLess(time.monotonic()-started,1.5)
        finally:os.close(r);os.close(w)

    def test_stage_identity_and_secret_shape_refuse_without_authoritative_file(self):
        self.assertIsNotNone(custody)
        for key in ('installation_id','tenant_id','project_id','client_ref'):
            changed=copy.deepcopy(self.expected);changed[key]='changed'
            with self.subTest(key=key),self.opened() as vault:
                with vault.seed_record() as record:
                    with self.assertRaises(ValueError):record.create(intent=changed,credential=SECRET)
        for raw in (b'',b'x'*65,b'x\n'*32):
            with self.subTest(size=len(raw)),self.opened() as vault:
                with vault.seed_record() as record:
                    with self.assertRaises(ValueError):record.create(intent=self.expected,credential=raw)
        self.assertFalse(self.leaf.exists())


if __name__=='__main__':unittest.main()
