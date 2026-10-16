"""X11-b source admission doubles + real IO in an owned temporary directory.

Only the fixed, currently UNBOUND supplier is replaced. ACL/authority/exclusion
facts are synthetic; source custody, destination IO and hashes are real. These
cases confer no installed-owner, native ACL or R01 authentication credit.
"""
from contextlib import contextmanager
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install.owner import OwnerRefusal
from cortex_core.release.custody import RootReader


SUBJECT = {'identity': {'install': 'one'}, 'effect': {'resource_kind': 'file'},
           'request_ref': {'path': 'request', 'bytes': 1, 'sha256': 'd' * 64}}


def facts(path):
    """Independent fixture observation, not the product snapshot helper."""
    if not path.exists() and not path.is_symlink():
        return None
    s = path.lstat()
    v = dict(dev=s.st_dev, ino=s.st_ino, uid=s.st_uid, gid=s.st_gid,
             mode=stat.S_IMODE(s.st_mode), nlink=s.st_nlink,
             mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns, acl=[], xattrs={})
    if path.is_symlink():
        return dict(v, kind='link', target=os.readlink(path))
    # Source fixture metadata collector below declares empty ACL/xattrs. Mac
    # stdlib lacks listxattr; no native extended-metadata measurement is claimed.
    if path.is_dir():
        return dict(v, kind='directory', children={p.name: facts(p) for p in sorted(path.iterdir())})
    raw = path.read_bytes()
    return dict(v, kind='file', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def file_node(raw=b'original member\n', source='members/payload'):
    return dict(kind='file', mode=0o600, bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), source=source)


class Inputs:
    def __init__(self, area):
        self.area = area
        self.root = area / 'destination'; self.root.mkdir(mode=0o700)
        self.source = area / 'originals'; (self.source / 'members').mkdir(parents=True)
        (self.source / 'members/payload').write_bytes(b'original member\n')
        (self.source / 'members/payload').chmod(0o600)
        self.rows = [{'path': 'members/payload', 'kind': 'record'}]
        self.reader = RootReader.for_aggregate(str(self.source), 2**20)
        self.limits = dict(member_count=100, record_bytes=2**20, image_bytes=2**20,
                           lock_bytes=2**20, aggregate_bytes=2**22, read_seconds=60)
        s = self.root.stat(); uid = os.getuid(); gid = os.getgid()
        self.binding = dict(root=str(self.root), root_identity=[s.st_dev, s.st_ino],
                            owner_uid=uid, owner_gid=gid, peer_uid=uid+1,
                            backend_uid=uid, clock_epoch='source-clock',
                            authority=['policy', 'consent', 'head', 1],
                            exclusion={'holder': 'fixture-owner', 'generation': 1,
                                       'participants': [uid], 'bypass_uids': []}, consumers=[])
        self.plan = dict(subject=copy.deepcopy(SUBJECT), deadline_ns=1000,
                         action='create', target='published', stage='.stage-one',
                         before=None, after=file_node(), ownership='created_by_this_transaction',
                         data_class='ephemeral', inverse=False,
                         expanded_bytes=2**20, member_count=100)
        self.now = 1; self.held = False; self.acl_value = []

    @contextmanager
    def hold(self):
        assert not self.held
        self.held = True
        try:
            self.reader.aggregate_begin(self.rows, self.limits)
            yield
        finally:
            self.reader.close(); self.held = False

    def current(self):
        assert self.held
        return copy.deepcopy(self.binding), self.now

    def metadata(self, parent, name, fd):
        assert self.held
        return dict(acl=copy.deepcopy(self.acl_value), xattrs={})


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='x11b-')
        self.addCleanup(self.tmp.cleanup)
        self.inputs = Inputs(Path(self.tmp.name).resolve())

    def api(self):
        try:
            return importlib.import_module('cortex_core.install.files')
        except ModuleNotFoundError:
            self.fail('X11B_SURFACE_ABSENT: required private files primitive is missing')

    @contextmanager
    def opened(self):
        api = self.api()
        with patch.object(api, '_installed_files', return_value=self.inputs):
            with api._open() as primitive:
                yield primitive

    def apply(self, primitive):
        primitive.snapshot()
        primitive.dispatch(copy.deepcopy(SUBJECT))
        with primitive.measure(copy.deepcopy(SUBJECT)) as answer:
            if answer[0] != 'UNKNOWN':
                self.assertEqual(set(json.loads(answer[1])), {'identity','effect','request_ref','state','observed'})
            return answer

    def archive(self):
        self.inputs.plan.update(action='materialize', after=dict(kind='directory', mode=0o700,
            children={'data': file_node(), 'empty': dict(kind='directory', mode=0o700, children={}),
                      'link': dict(kind='link', mode=0o777, target='data')}))

    def existing(self, action='replace'):
        path = self.inputs.root / 'published'
        path.write_bytes(b'before'); path.chmod(0o600)
        self.inputs.plan.update(action=action, before=facts(path), inverse=action=='remove',
                                after=None if action=='remove' else file_node())
        return path

    def test_installed_supplier_refuses_before_io(self):
        api = self.api()
        with patch.object(api.os, 'open', side_effect=AssertionError('candidate accessed')):
            with self.assertRaises(OwnerRefusal) as raised:
                with api._open(): pass
            self.assertEqual(raised.exception.detail, 'required_binding_gap')

    def test_file_create_and_original_result(self):
        with self.opened() as p:
            answer = self.apply(p)
            self.assertEqual(answer[0], 'APPLIED')
            self.assertEqual((self.inputs.root/'published').read_bytes(), b'original member\n')
            self.assertEqual(json.loads(answer[1])['observed']['resource'], facts(self.inputs.root/'published'))
            self.assertFalse((self.inputs.root/'.stage-one').exists())
            self.assertTrue(self.inputs.held)
        self.assertFalse(self.inputs.held)

    def test_archive_complete_directory_link_and_empty_directory(self):
        self.archive()
        with self.opened() as p:
            self.assertEqual(self.apply(p)[0], 'APPLIED')
        dest = self.inputs.root/'published'
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['data','empty','link'])
        self.assertEqual(os.readlink(dest/'link'), 'data')
        self.assertEqual((dest/'data').stat().st_mode & 0o777, 0o600)

    def test_nested_parent_is_held(self):
        (self.inputs.root/'sub').mkdir(mode=0o700)
        self.inputs.plan['target'] = 'sub/published'
        with self.opened() as p:
            self.assertEqual(self.apply(p)[0], 'APPLIED')

    def test_exact_owned_replacement(self):
        self.existing()
        with self.opened() as p:
            self.assertEqual(self.apply(p)[0], 'APPLIED')

    def test_exact_ephemeral_inverse(self):
        path = self.existing('remove')
        with self.opened() as p:
            self.assertEqual(self.apply(p)[0], 'APPLIED')
        self.assertFalse(path.exists())

    def test_recursive_inverse_of_owned_archive(self):
        self.archive()
        with self.opened() as p: self.assertEqual(self.apply(p)[0], 'APPLIED')
        self.inputs.plan.update(action='remove', before=facts(self.inputs.root/'published'), after=None, inverse=True)
        with self.opened() as p: self.assertEqual(self.apply(p)[0], 'APPLIED')
        self.assertFalse((self.inputs.root/'published').exists())

    def test_source_hash_mismatch_precedes_any_destination_effect(self):
        self.inputs.plan['after']['sha256'] = '0'*64
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(list(self.inputs.root.iterdir()), [])

    def test_source_named_substitution_refuses(self):
        with self.opened() as p:
            p.snapshot()
            source = self.inputs.source/'members/payload'
            source.rename(source.with_name('saved')); source.write_bytes(b'original member\n')
            with self.assertRaises(OwnerRefusal): p.dispatch(SUBJECT)
        self.assertEqual(list(self.inputs.root.iterdir()), [])

    def test_destination_path_escape_refused(self):
        self.inputs.plan['target'] = '../outside'
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertFalse((self.inputs.area/'outside').exists())

    def test_stage_alias_and_preexisting_stage_refused(self):
        self.inputs.plan['stage'] = 'published'
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.inputs.plan['stage'] = '.stage-one'; (self.inputs.root/'.stage-one').write_bytes(b'foreign')
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual((self.inputs.root/'.stage-one').read_bytes(), b'foreign')

    def test_archive_escape_link_refused(self):
        self.archive(); self.inputs.plan['after']['children']['link']['target'] = '../outside'
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(list(self.inputs.root.iterdir()), [])

    def test_member_count_and_expanded_bytes_bounded(self):
        self.archive(); self.inputs.plan['member_count'] = 1
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.inputs.plan['member_count'] = 100; self.inputs.plan['expanded_bytes'] = 1
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(list(self.inputs.root.iterdir()), [])

    def test_hardlink_preimage_refused(self):
        path = self.existing(); os.link(path, self.inputs.root/'alias')
        self.inputs.plan['before'] = facts(path)
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(path.read_bytes(), b'before')

    def test_shared_external_data_and_consumed_inverse_refused(self):
        path = self.existing('remove')
        for field, value in [('ownership','external'), ('ownership','shared'), ('data_class','authoritative_data'),
                             ('data_class','credential_store'), ('inverse',False)]:
            original = self.inputs.plan[field]; self.inputs.plan[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(OwnerRefusal):
                with self.opened() as p: self.apply(p)
            self.inputs.plan[field] = original
        self.inputs.binding['consumers'] = ['other']
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(path.read_bytes(), b'before')

    def test_changed_before_refuses_inverse(self):
        path = self.existing('remove'); path.write_bytes(b'changed')
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(path.read_bytes(), b'changed')

    def test_parent_swap_after_snapshot_refuses(self):
        sub = self.inputs.root/'sub'; sub.mkdir(mode=0o700); self.inputs.plan['target']='sub/published'
        with self.opened() as p:
            p.snapshot(); sub.rename(self.inputs.root/'old'); sub.mkdir(mode=0o700)
            with self.assertRaises(OwnerRefusal): p.dispatch(SUBJECT)
        self.assertFalse((sub/'published').exists())

    def test_root_swap_during_publication_refuses(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            with self.assertRaises(OwnerRefusal):
                with p.measure(SUBJECT) as answer:
                    self.assertEqual(answer[0], 'APPLIED')
                    root = self.inputs.root; root.rename(root.with_name('saved')); root.mkdir(mode=0o700)

    def test_target_aba_same_bytes_is_unknown(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            path=self.inputs.root/'published'; path.rename(self.inputs.root/'saved')
            path.write_bytes(b'original member\n'); path.chmod(0o600)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer, ('UNKNOWN',None))

    def test_partial_write_retains_stage_and_is_unknown(self):
        api=self.api(); real=api.os.write
        def short(fd, raw):
            real(fd, raw[:2]); raise OSError('lost write reply')
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'write',side_effect=short): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer, ('UNKNOWN',None))
        self.assertTrue((self.inputs.root/'.stage-one').exists())
        self.assertFalse((self.inputs.root/'published').exists())

    def test_fsync_failure_is_unknown_without_false_ack(self):
        api=self.api()
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'fsync',side_effect=OSError('uncertain durability')): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer, ('UNKNOWN',None))
        self.assertTrue((self.inputs.root/'.stage-one').exists())

    def test_pre_effect_failure_proves_not_applied(self):
        api=self.api(); real=api.os.open
        def fail_stage(path, flags, *a, **kw):
            if path == '.stage-one' and flags & os.O_CREAT: raise OSError('no create')
            return real(path,flags,*a,**kw)
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'open',side_effect=fail_stage): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer[0],'NOT_APPLIED')

    def test_lost_publish_reply_independent_measurement_applies(self):
        api=self.api(); real=api.os.rename
        def lost(*a,**kw): real(*a,**kw); raise OSError('lost rename reply')
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'rename',side_effect=lost): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer[0],'APPLIED')

    def test_foreign_extra_member_blocks_publish(self):
        self.archive(); api=self.api(); real=api.os.fsync; once=[]
        def inject(fd):
            real(fd)
            stage=self.inputs.root/'.stage-one'
            if stage.is_dir() and not once:
                once.append(True); (stage/'foreign').write_bytes(b'foreign')
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'fsync',side_effect=inject): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
        self.assertFalse((self.inputs.root/'published').exists())

    def test_revocation_during_io_never_verifies(self):
        api=self.api(); real=api.os.write
        def revoke(fd, raw):
            result=real(fd,raw); self.inputs.binding['authority'][3]+=1; return result
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'write',side_effect=revoke): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))

    def test_original_deadline_equals_now_refuses(self):
        self.inputs.now=1000
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(list(self.inputs.root.iterdir()),[])

    def test_expiry_during_measurement_final_io_refuses(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            with self.assertRaises(OwnerRefusal):
                with p.measure(SUBJECT) as answer:
                    self.assertEqual(answer[0],'APPLIED'); self.inputs.now=1000

    def test_acl_drift_is_unknown(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT); self.inputs.acl_value=['unexpected-allow']
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))

    def test_publication_failure_propagates(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            with self.assertRaisesRegex(RuntimeError,'publisher failed'):
                with p.measure(SUBJECT) as answer:
                    self.assertEqual(answer[0],'APPLIED'); raise RuntimeError('publisher failed')

    def test_one_dispatch_exact_subject_and_no_measure_dispatch(self):
        with self.opened() as p:
            p.snapshot()
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
            wrong=copy.deepcopy(SUBJECT); wrong['identity']['install']='other'
            with self.assertRaises(OwnerRefusal): p.dispatch(wrong)
            p.dispatch(SUBJECT)
            with self.assertRaises(OwnerRefusal): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer[0],'APPLIED')
            with p.measure(SUBJECT) as again: self.assertEqual(again,answer)

    def test_link_preimage_replaced_without_following_target(self):
        outside=self.inputs.area/'outside'; outside.write_bytes(b'retain')
        path=self.inputs.root/'published'; path.symlink_to('../outside')
        self.inputs.plan.update(action='replace',before=facts(path))
        with self.opened() as p: self.assertEqual(self.apply(p)[0],'APPLIED')
        self.assertEqual(outside.read_bytes(),b'retain')

    def test_verified_owned_continuation_replace_keeps_provenance(self):
        self.existing(); self.inputs.plan['ownership']='verified_owned_continuation'
        try:
            with self.opened() as p:
                answer=self.apply(p); self.assertEqual(answer[0],'APPLIED')
                self.assertEqual(json.loads(answer[1])['observed']['ownership'],'verified_owned_continuation')
        except OwnerRefusal as exc:
            self.fail('X11B_CONTINUATION_REFUSED: '+exc.detail)

    def test_verified_owned_continuation_is_not_transaction_inverse(self):
        path=self.existing('remove'); self.inputs.plan['ownership']='verified_owned_continuation'
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(path.read_bytes(),b'before')

    def test_inverse_directory_metadata_change_during_fsync_retains_it(self):
        path=self.inputs.root/'published'; path.mkdir(mode=0o700)
        self.inputs.plan.update(action='remove',before=facts(path),after=None,inverse=True)
        api=self.api(); real=api.os.fsync; identity=path.stat().st_ino
        def drift(fd):
            real(fd)
            if os.fstat(fd).st_ino==identity: os.fchmod(fd,0o750)
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'fsync',side_effect=drift): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
        self.assertTrue(path.is_dir())

    def test_large_member_short_writes_keep_original_bytes_and_chunk_bound(self):
        raw=bytes(range(256))*1100
        (self.inputs.source/'members/payload').write_bytes(raw)
        self.inputs.plan['after']=file_node(raw)
        api=self.api(); real=api.os.write; sizes=[]
        def short(fd,view):
            sizes.append(len(view)); return real(fd,view[:max(1,len(view)//2)])
        with self.opened() as p:
            with patch.object(api.os,'write',side_effect=short):
                self.assertEqual(self.apply(p)[0],'APPLIED')
            self.assertTrue(p.fds)
            self.assertEqual(p.writers,set())
        self.assertGreater(len(sizes),4); self.assertLessEqual(max(sizes),65536)
        self.assertEqual((self.inputs.root/'published').read_bytes(),raw)

    def test_standalone_directory_create(self):
        self.inputs.plan['after']=dict(kind='directory',mode=0o700,children={})
        with self.opened() as p: self.assertEqual(self.apply(p)[0],'APPLIED')
        self.assertTrue((self.inputs.root/'published').is_dir())

    def test_standalone_admitted_link_create_without_target_traversal(self):
        peer=self.inputs.root/'peer'; peer.write_bytes(b'retain')
        self.inputs.plan['after']=dict(kind='link',mode=0o777,target='peer')
        with self.opened() as p: self.assertEqual(self.apply(p)[0],'APPLIED')
        self.assertEqual(os.readlink(self.inputs.root/'published'),'peer')
        self.assertEqual(peer.read_bytes(),b'retain')

    def test_identical_existing_file_is_not_new_ownership(self):
        path=self.inputs.root/'published'; path.write_bytes(b'original member\n'); path.chmod(0o600)
        before=facts(path)
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(facts(path),before)

    def test_file_drift_during_final_parent_fsync_is_unknown(self):
        api=self.api(); real=api.os.fsync; path=self.inputs.root/'published'; done=[]
        def drift(fd):
            real(fd)
            if path.exists() and not done:
                done.append(True); path.write_bytes(b'substituted body')
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'fsync',side_effect=drift): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
        self.assertEqual(path.read_bytes(),b'substituted body')

    def test_metadata_change_during_publication_refuses_ack(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            with self.assertRaises(OwnerRefusal):
                with p.measure(SUBJECT) as answer:
                    self.assertEqual(answer[0],'APPLIED')
                    (self.inputs.root/'published').chmod(0o700)

    def test_consumer_change_during_publication_refuses_ack(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            with self.assertRaises(OwnerRefusal):
                with p.measure(SUBJECT) as answer:
                    self.assertEqual(answer[0],'APPLIED')
                    self.inputs.binding['consumers']=['new-consumer']

    def test_fifo_preimage_never_opens_or_blocks(self):
        path=self.inputs.root/'published'; os.mkfifo(path,0o600)
        self.inputs.plan.update(action='replace',before={'kind':'file'})
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))

    def test_empty_owned_directory_atomic_replace(self):
        path=self.inputs.root/'published'; path.mkdir(mode=0o700)
        before=facts(path); self.archive()
        self.inputs.plan.update(action='replace',before=before)
        with self.opened() as p: self.assertEqual(self.apply(p)[0],'APPLIED')
        self.assertNotEqual(path.stat().st_ino,before['ino'])

    def test_nonempty_directory_exchange_refuses_without_native_binding(self):
        path=self.inputs.root/'published'; path.mkdir(mode=0o700)
        (path/'data').write_bytes(b'retain'); (path/'data').chmod(0o600)
        before=facts(path); self.archive(); self.inputs.plan.update(action='replace',before=before)
        with self.assertRaises(OwnerRefusal):
            with self.opened() as p: self.apply(p)
        self.assertEqual(facts(path),before)

    def test_failed_inverse_before_unlink_measures_not_applied(self):
        path=self.existing('remove'); api=self.api()
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'unlink',side_effect=OSError('no effect')): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer[0],'NOT_APPLIED')
        self.assertEqual(path.read_bytes(),b'before')

    def test_partial_inverse_retains_remaining_members_and_is_unknown(self):
        path=self.inputs.root/'published'; path.mkdir(mode=0o700)
        for name in ('a','b'): (path/name).write_bytes(b'retain'); (path/name).chmod(0o600)
        self.inputs.plan.update(action='remove',before=facts(path),after=None,inverse=True)
        api=self.api(); real=api.os.unlink
        def lost(*a,**kw): real(*a,**kw); raise OSError('lost unlink reply')
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'unlink',side_effect=lost): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
        self.assertEqual((path/'b').read_bytes(),b'retain')

    def test_source_change_after_publish_retains_effect_and_is_unknown(self):
        with self.opened() as p:
            p.snapshot(); p.dispatch(SUBJECT)
            (self.inputs.source/'members/payload').write_bytes(b'revoked original')
            with p.measure(SUBJECT) as answer: self.assertEqual(answer,('UNKNOWN',None))
        self.assertEqual((self.inputs.root/'published').read_bytes(),b'original member\n')

    def test_not_applied_does_not_permit_a_second_dispatch(self):
        api=self.api(); real=api.os.open
        def fail_stage(path,flags,*a,**kw):
            if path=='.stage-one' and flags & os.O_CREAT: raise OSError('no effect')
            return real(path,flags,*a,**kw)
        with self.opened() as p:
            p.snapshot()
            with patch.object(api.os,'open',side_effect=fail_stage): p.dispatch(SUBJECT)
            with p.measure(SUBJECT) as answer: self.assertEqual(answer[0],'NOT_APPLIED')
            with self.assertRaises(OwnerRefusal): p.dispatch(SUBJECT)
        self.assertEqual(list(self.inputs.root.iterdir()),[])


if __name__ == '__main__':
    unittest.main()
