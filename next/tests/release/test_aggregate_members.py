"""U3-C deciding custody oracle, frozen RED before source; fixture bytes only."""
import copy
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aggregate_fixture import materials,write,encode,sha256
from cortex_core.runtime.signed_member import Refusal

SRC=Path(__file__).resolve().parents[2]/'src/cortex_core/release'
MAIN=Path('/Users/amadmalik/DevVault/helix/output/kos/ren-kos-u3c-fixtures-20261014')


class Members(unittest.TestCase):
    def source(self):
        self.assertTrue((SRC/'aggregate.py').is_file(),'SURFACE_ABSENCE_BODY: U3-C aggregate source absent')
        return importlib.import_module('cortex_core.release.aggregate'),importlib.import_module('cortex_core.release.custody')

    def setUp(self):
        MAIN.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='inert-',dir=MAIN.resolve())
        self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.value,self.bodies,self.builds,self.policy=materials()
        write(self.root,self.bodies)

    def reader(self):
        _,c=self.source();r=c.RootReader.for_aggregate(str(self.root),8*1024**2)
        self.addCleanup(r.close);return r

    def begin(self,r,limits=None):
        a,_=self.source();r.aggregate_begin(self.value['members'],limits or a.PROVISIONAL_LIMITS)

    def refuse(self,reason,fn):
        with self.assertRaises(Refusal) as caught:fn()
        self.assertEqual(caught.exception.reason,reason)

    def path(self,kind='record'):
        return next(r['path'] for r in self.value['members'] if r['kind']==kind)

    def test_all_original_members_exact_and_no_descriptor_survives_close(self):
        r=self.reader();self.begin(r)
        for path,raw in self.bodies.items():self.assertEqual(r.aggregate_bytes(path),raw)
        held=[x[0] for x in r._aggregate_files.values()]
        r.aggregate_finish();r.close()
        for fd in held:
            with self.assertRaises(OSError):os.fstat(fd)
        self.assertIsNone(r.fd)

    def test_T03_same_length_changed_body_refuses_digest(self):
        a,_=self.source();p=self.path();raw=self.bodies[p]
        (self.root/p).write_bytes(bytes([raw[0]^1])+raw[1:])
        self.refuse('member_digest_mismatch',lambda:a._measure_members(self.value,self.reader(),a.PROVISIONAL_LIMITS))

    def test_T03_truncated_body_refuses_digest(self):
        a,_=self.source();p=self.path();(self.root/p).write_bytes(self.bodies[p][:-1])
        self.refuse('member_digest_mismatch',lambda:a._measure_members(self.value,self.reader(),a.PROVISIONAL_LIMITS))

    def test_T08_all_lexical_member_aliases_before_open(self):
        a,_=self.source()
        for path in ('/tmp/a','members/../a','members//a','members/./a','members\\a','C:/a','members/A','members/%61','members/é','runtime.lock.json/'):
            with self.subTest(path=path):
                v=copy.deepcopy(self.value);v['members'][0]['path']=path
                r=self.reader()
                with patch.object(r,'aggregate_begin',side_effect=AssertionError('opened aliased member')):
                    self.refuse('member_path_alias',lambda:a._measure_members(v,r,a.PROVISIONAL_LIMITS))

    def test_T08_root_dot_alias_before_physical_open(self):
        _,c=self.source();r=c.RootReader.for_aggregate(str(self.root)+'/../'+self.root.name,8*1024**2)
        self.addCleanup(r.close)
        self.refuse('member_path_alias',lambda:self.begin(r))
        self.assertIsNone(r.fd)

    def test_T08_root_case_alias(self):
        _,c=self.source();r=c.RootReader.for_aggregate(str(self.root.parent)+'/'+self.root.name.upper(),8*1024**2)
        self.addCleanup(r.close)
        self.refuse('member_path_alias',lambda:self.begin(r))

    def test_T09_file_symlink(self):
        p=self.path();f=self.root/p;raw=f.read_bytes();f.unlink();outside=self.root.parent/(self.root.name+'-outside');outside.write_bytes(raw)
        self.addCleanup(outside.unlink);f.symlink_to(outside)
        self.refuse('member_path_alias',lambda:self.begin(self.reader()))

    def test_T09_hardlink(self):
        p=self.path();outside=self.root.parent/(self.root.name+'-hard');os.link(self.root/p,outside);self.addCleanup(outside.unlink)
        self.refuse('member_path_alias',lambda:self.begin(self.reader()))

    def test_T09_root_symlink(self):
        _,c=self.source();alias=self.root.parent/(self.root.name+'-alias');alias.symlink_to(self.root,target_is_directory=True);self.addCleanup(alias.unlink)
        r=c.RootReader.for_aggregate(str(alias),8*1024**2);self.addCleanup(r.close)
        self.refuse('member_path_alias',lambda:self.begin(r))

    def test_T09_member_parent_symlink(self):
        directory=self.root/'members/contracts';moved=self.root.parent/(self.root.name+'-contracts');directory.rename(moved)
        self.addCleanup(lambda:moved.rename(directory) if moved.exists() else None)
        self.addCleanup(directory.unlink);directory.symlink_to(moved,target_is_directory=True)
        self.refuse('member_path_alias',lambda:self.begin(self.reader()))

    def test_T09_file_replaced_after_hold(self):
        r=self.reader();self.begin(r);p=self.path();f=self.root/p;raw=f.read_bytes();f.unlink();f.write_bytes(raw)
        self.refuse('member_custody_changed',lambda:r.aggregate_bytes(p))

    def test_T09_root_replaced_after_hold(self):
        r=self.reader();self.begin(r);moved=self.root.parent/(self.root.name+'-old');self.root.rename(moved);self.root.mkdir()
        self.addCleanup(lambda:moved.rename(self.root) if moved.exists() else None);self.addCleanup(self.root.rmdir)
        self.refuse('member_custody_changed',lambda:r.aggregate_bytes(self.path()))

    def test_T09_member_ancestor_replaced_after_hold(self):
        r=self.reader();self.begin(r);old=self.root/'members';moved=self.root/'held-old';old.rename(moved);old.mkdir()
        self.addCleanup(lambda:moved.rename(old) if moved.exists() else None);self.addCleanup(old.rmdir)
        self.refuse('member_custody_changed',lambda:r.aggregate_bytes(self.path()))

    def test_T09_in_place_change_after_hold(self):
        r=self.reader();self.begin(r);p=self.path();(self.root/p).write_bytes(self.bodies[p])
        self.refuse('member_custody_changed',lambda:r.aggregate_bytes(p))

    def test_T09_change_during_stream(self):
        _,c=self.source();r=self.reader();self.begin(r);p=self.path();real=c.os.read
        def read(fd,count):
            raw=real(fd,count)
            if raw:(self.root/p).write_bytes(self.bodies[p])
            return raw
        with patch.object(c.os,'read',read):self.refuse('member_custody_changed',lambda:r.aggregate_bytes(p))

    def test_T30_final_rechecks_every_held_subject(self):
        r=self.reader();self.begin(r)
        for p in self.bodies:r.aggregate_bytes(p)
        p=self.path();(self.root/p).write_bytes(self.bodies[p])
        self.refuse('member_custody_changed',r.aggregate_finish)

    def test_T19_missing_original_no_partial_files(self):
        r=self.reader();(self.root/self.path()).unlink()
        self.refuse('required_binding_gap',lambda:self.begin(r));self.assertIsNone(r.fd);self.assertFalse(r._aggregate_files)

    def test_extra_physical_file_refuses(self):
        (self.root/'members/undeclared').write_bytes(b'inert')
        self.refuse('aggregate_pair_mismatch',lambda:self.begin(self.reader()))

    def test_extra_empty_directory_refuses(self):
        (self.root/'members/undeclared').mkdir()
        self.refuse('aggregate_pair_mismatch',lambda:self.begin(self.reader()))

    def test_extra_file_after_hold_refuses_finish(self):
        r=self.reader();self.begin(r);(self.root/'members/undeclared').write_bytes(b'inert')
        self.refuse('aggregate_pair_mismatch',r.aggregate_finish)

    def test_T22_measured_record_bound_not_declared(self):
        a,_=self.source();r=self.reader();limits=dict(a.PROVISIONAL_LIMITS,record_bytes=1)
        self.refuse('raw_limit',lambda:self.begin(r,limits));self.assertIsNone(r.fd)

    def test_T22_measured_image_bound(self):
        a,_=self.source();r=self.reader();limits=dict(a.PROVISIONAL_LIMITS,image_bytes=1)
        self.refuse('raw_limit',lambda:self.begin(r,limits))

    def test_T22_measured_lock_bound(self):
        a,_=self.source();r=self.reader();limits=dict(a.PROVISIONAL_LIMITS,lock_bytes=1)
        self.refuse('raw_limit',lambda:self.begin(r,limits))

    def test_T22_measured_total_includes_overhead(self):
        a,_=self.source();r=self.reader();total=sum(map(len,self.bodies.values()))
        self.refuse('raw_limit',lambda:r.aggregate_begin(self.value['members'],dict(a.PROVISIONAL_LIMITS,aggregate_bytes=total),overhead_bytes=1))

    def test_T22_stream_is_chunk_bounded(self):
        _,c=self.source();r=self.reader();self.begin(r);real=c.os.read;counts=[]
        def read(fd,count):counts.append(count);return real(fd,count)
        with patch.object(c.os,'read',read):self.assertEqual(r.aggregate_bytes(self.path('image')),self.bodies[self.path('image')])
        self.assertTrue(counts);self.assertLessEqual(max(counts),65536)

    def test_T22_time_during_stream_refuses(self):
        _,c=self.source();r=self.reader();self.begin(r)
        with patch.object(c.time,'monotonic',return_value=r._aggregate_deadline+1):
            self.refuse('raw_limit',lambda:r.aggregate_bytes(self.path()))

    def test_unsigned_physical_record_cannot_be_requested(self):
        r=self.reader();self.begin(r)
        self.refuse('member_not_in_aggregate',lambda:r.aggregate_bytes('members/absent'))

    def test_fifo_is_rejected_without_blocking(self):
        p=self.path();f=self.root/p;f.unlink();os.mkfifo(f)
        self.refuse('member_path_alias',lambda:self.begin(self.reader()))


class CustodyControls(unittest.TestCase):
    """Causal companions; no original deciding assertion is edited."""
    source=Members.source;setUp=Members.setUp;reader=Members.reader;begin=Members.begin
    refuse=Members.refuse;path=Members.path

    def test_root_alias_refuses_before_any_physical_open(self):
        _,c=self.source();r=c.RootReader.for_aggregate(str(self.root)+'/../'+self.root.name,8*1024**2);self.addCleanup(r.close)
        calls=[];real=c.os.open
        def observed(*args,**kwargs):calls.append(args[0]);return real(*args,**kwargs)
        with patch.object(c.os,'open',observed):self.refuse('member_path_alias',lambda:self.begin(r))
        self.assertEqual(calls,[])

    def test_directory_replaced_between_stat_and_open(self):
        import shutil
        _,c=self.source();r=self.reader();real=c.os.open
        backup=self.root.parent/(self.root.name+'-held');replacement=self.root.parent/(self.root.name+'-replacement')
        shutil.copytree(self.root,replacement);changed=[]
        def opened(name,flags,**kwargs):
            if name==self.root.name and not changed:
                self.root.rename(backup);replacement.rename(self.root);changed.append(True)
            return real(name,flags,**kwargs)
        def restore():
            if backup.exists():shutil.rmtree(self.root);backup.rename(self.root)
            if replacement.exists():shutil.rmtree(replacement)
        self.addCleanup(restore)
        with patch.object(c.os,'open',opened):self.refuse('member_custody_changed',lambda:self.begin(r))
        self.assertEqual(changed,[True]);self.assertIsNone(r.fd)

    def test_final_hash_despite_simulated_stale_stat_observation(self):
        # Fault injection models stale/coarse metadata; actual held-FD body read
        # is unchanged. This is source-only, not native kernel tamper proof.
        _,c=self.source();r=self.reader();self.begin(r);p=self.path();r.aggregate_bytes(p)
        fd,parent,name,before,bound,old_hash=r._aggregate_files[p]
        raw=self.bodies[p];(self.root/p).write_bytes(bytes([raw[0]^1])+raw[1:])
        real_stat=c.os.stat;real_fstat=c.os.fstat
        def stat(path,*args,**kwargs):
            if path==name and kwargs.get('dir_fd')==parent:return before
            return real_stat(path,*args,**kwargs)
        def fstat(f):return before if f==fd else real_fstat(f)
        with patch.object(c.os,'stat',stat),patch.object(c.os,'fstat',fstat):
            self.refuse('member_custody_changed',r.aggregate_finish)

    def test_count_bound_in_direct_custody(self):
        a,_=self.source();r=self.reader();self.refuse('raw_limit',lambda:self.begin(r,dict(a.PROVISIONAL_LIMITS,member_count=1)))

    def test_duplicate_path_in_direct_custody(self):
        a,_=self.source();r=self.reader();rows=self.value['members']+[self.value['members'][0]]
        self.refuse('aggregate_shape',lambda:r.aggregate_begin(rows,a.PROVISIONAL_LIMITS))

    def test_declared_deep_prefixes_are_not_an_invented_member_cap(self):
        a,c=self.source();root=self.root/'isolated';path='members/a/b/c/d/e/subject';raw=b'inert\n'
        target=root/path;target.parent.mkdir(parents=True);target.write_bytes(raw)
        r=c.RootReader.for_aggregate(str(root),8*1024**2);self.addCleanup(r.close)
        row=dict(kind='record',role='evidence',path=path,media_type='application/octet-stream',bytes=len(raw),sha256=sha256(raw))
        try:r.aggregate_begin([row],dict(a.PROVISIONAL_LIMITS,member_count=1))
        except Refusal as e:self.fail('legal declared prefixes refused: '+e.reason)
        self.assertEqual(r.aggregate_bytes(path),raw);r.aggregate_finish()

    def test_close_closes_all_held_file_directory_and_root_descriptors(self):
        r=self.reader();self.begin(r);fds=[x[0] for x in r._aggregate_files.values()]+list(r._aggregate_dirs.values())+[r.fd]
        r.close()
        for fd in fds:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_rebegin_closes_previous_transaction_before_first_open(self):
        _,c=self.source();r=self.reader();self.begin(r);fds={x[0] for x in r._aggregate_files.values()}|set(r._aggregate_dirs.values())|{r.fd}
        closed=[];real_close=c.os.close;real_open=c.os.open;seen=[]
        def close(fd):closed.append(fd);return real_close(fd)
        def opened(*args,**kwargs):
            if not seen:self.assertTrue(fds<=set(closed));seen.append(True)
            return real_open(*args,**kwargs)
        with patch.object(c.os,'close',close),patch.object(c.os,'open',opened):self.begin(r)
        self.assertEqual(seen,[True])

    def test_file_NOFOLLOW_prevents_returning_outside_descriptor_during_race(self):
        _,c=self.source();r=self.reader();p=self.path();name=Path(p).name;outside=self.root.parent/(self.root.name+'-outside-race')
        real=c.os.open;changed=[];opened=[];self.addCleanup(lambda:outside.unlink(missing_ok=True))
        def open_file(part,flags,**kwargs):
            if part==name and not changed:
                (self.root/p).rename(outside);(self.root/p).symlink_to(outside);changed.append(True)
                fd=real(part,flags,**kwargs);opened.append(fd);return fd
            return real(part,flags,**kwargs)
        with patch.object(c.os,'open',open_file):self.refuse('member_path_alias',lambda:self.begin(r))
        self.assertEqual(changed,[True]);self.assertEqual(opened,[])

    def test_stream_growth_measured_cap_precedes_stat_drift(self):
        a,c=self.source();root=self.root/'isolated';p='members/subject';raw=b'inert';target=root/p;target.parent.mkdir(parents=True);target.write_bytes(raw)
        r=c.RootReader.for_aggregate(str(root),len(raw));self.addCleanup(r.close)
        row=dict(kind='record',role='evidence',path=p,bytes=len(raw),sha256=sha256(raw),media_type='application/octet-stream')
        r.aggregate_begin([row],dict(a.PROVISIONAL_LIMITS,record_bytes=len(raw)));real=c.os.read;changed=[]
        def read(fd,count):
            data=real(fd,count)
            if data and not changed:
                with target.open('ab') as f:f.write(b'growth')
                changed.append(True)
            return data
        with patch.object(c.os,'read',read):self.refuse('raw_limit',lambda:r.aggregate_bytes(p))

    def test_premature_empty_read_is_custody_refusal(self):
        _,c=self.source();r=self.reader();self.begin(r)
        with patch.object(c.os,'read',return_value=b''):self.refuse('member_custody_changed',lambda:r.aggregate_bytes(self.path()))
