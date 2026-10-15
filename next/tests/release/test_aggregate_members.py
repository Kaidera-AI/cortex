"""U3-C deciding custody oracle, frozen RED before source; fixture bytes only."""
import copy
import importlib
import os
from pathlib import Path
from r01u3_support.workspace import workspace_output
import tempfile
import unittest
from unittest.mock import patch

from aggregate_fixture import materials,write,encode,sha256
from cortex_core.runtime.signed_member import Refusal

SRC=Path(__file__).resolve().parents[2]/'src/cortex_core/release'


MAIN=workspace_output()

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

    def test_workspace_storage_is_private_canonical_unique_and_cleaned(self):
        # BODY regression: storage belongs to this checkout, never a developer home.
        import stat
        self.assertIsNotNone(importlib.util.find_spec("r01u3_support.workspace"),
                             "BODY: portable fixture storage helper absent")
        from r01u3_support.workspace import workspace_output, temporary_directory
        base=workspace_output()
        checkout=Path(__file__).resolve().parents[3]
        project=checkout.parent.parent if checkout.parent.name=='.worktrees' else checkout
        self.assertEqual(base,project/'output/r01u3-fixtures')
        self.assertTrue(base.is_absolute());self.assertEqual(base,base.resolve(strict=True))
        self.assertEqual(stat.S_IMODE(base.stat().st_mode),0o700)
        with temporary_directory('portable-') as a, temporary_directory('portable-') as b:
            first,second=Path(a),Path(b)
            self.assertNotEqual(first,second)
            for path in (first,second):
                self.assertEqual(path.parent,base)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)
                (path/'inert').write_bytes(b'fixture only')
        self.assertFalse(first.exists());self.assertFalse(second.exists())

    def test_all_original_members_exact_and_no_descriptor_survives_close(self):
        r=self.reader();self.begin(r)
        for path,raw in self.bodies.items():self.assertEqual(r.aggregate_bytes(path),raw)
        held=[x[0] for x in r._aggregate_files.values()]
        r.aggregate_finish();r.close()
        for fd in held:
            with self.assertRaises(OSError):os.fstat(fd)
        self.assertIsNone(r.fd)

    def test_contains_required_tag_bounds_candidate_validation_work(self):
        from cortex_core.release.codec import WireValidator
        schema={'contains':{'properties':{'role':{'const':'required'}},'required':['role']}}
        rows=[{'role':'other'} for _ in range(511)]+[{'role':'required'}]
        actual=WireValidator.is_valid;calls=[]
        def valid(validator,*a,**k):calls.append(None);return actual(validator,*a,**k)
        with patch.object(WireValidator,'is_valid',valid):
            self.assertTrue(WireValidator(schema).is_valid(rows))
        self.assertLessEqual(len(calls),10,'BODY: impossible contains candidates revalidated')

    def test_contains_prefilter_preserves_general_schema_semantics(self):
        from cortex_core.release.codec import WireValidator
        from jsonschema import Draft202012Validator
        import itertools
        values=[{'role':'required'},{'kind':'record','role':'required'},
                {'role':'other'},{},{'role':None},'nonobject',None,True,0,['array']]
        clauses=[{'properties':{'role':{'const':'required'}},'required':['role']},
                 {'properties':{'role':{'const':'required'},'kind':{'const':'record'}},'required':['kind','role']},
                 {'properties':{'role':{'const':'required'}}},
                 {'properties':{'role':{'const':1}},'required':['role']}]
        arrays=[[],*[[v] for v in values],*[list(pair) for pair in itertools.product(values,repeat=2)]]
        for clause in clauses:
            for minimum,maximum in [(1,None),(0,0),(2,2),(0,1)]:
                schema={'contains':clause,'minContains':minimum}
                if maximum is not None:schema['maxContains']=maximum
                for array in arrays:
                    expected=Draft202012Validator(schema).is_valid(array)
                    actual=WireValidator(schema).is_valid(array)
                    if actual!=expected:self.fail(f'contains semantic mismatch: {schema} {array}')

    def test_wire_character_rules_match_original_for_every_unicode_codepoint(self):
        import unicodedata
        from cortex_core.release.codec import _strings
        # Independent literal previous rule, exhaustive over Python Unicode values.
        for point in range(0x110000):
            text=chr(point)
            for payload in (False,True):
                expected=('unicode_not_nfc' if not payload and unicodedata.normalize('NFC',text)!=text
                          else 'forbidden_unicode' if ((point<32 and not (payload and text in '\n\r\t'))
                                or 127<=point<=159 or 0xD800<=point<=0xDFFF) else None)
                actual=None
                try:_strings(text,text_payload=payload)
                except ValueError as error:actual=str(error)
                if actual!=expected:self.fail(f'character rule mismatch U+{point:04X} payload={payload}: {actual} != {expected}')

    def test_unique_path_rows_avoid_pairwise_object_comparisons(self):
        from cortex_core.release.codec import WireValidator
        import jsonschema._utils as utilities
        actual=utilities.equal;calls=[]
        def equal(a,b):calls.append(None);return actual(a,b)
        rows=[dict(path='members/record-'+str(i),payload={'inert':True}) for i in range(128)]
        with patch.object(utilities,'equal',equal):
            self.assertTrue(WireValidator({'uniqueItems':True}).is_valid(rows))
        self.assertLessEqual(len(calls),len(rows),'BODY: quadratic unique path-row comparisons')

    def test_wire_uniqueness_preserves_general_schema_semantics(self):
        from cortex_core.release.codec import WireValidator
        from jsonschema import Draft202012Validator
        for values in ([],[True,1],[False,0],[1,1.0],[[1],[1.0]],
                       [{'path':'same','a':1},{'path':'same','a':2}],
                       [{'path':'same','a':1},{'a':1,'path':'same'}],
                       [{'path':'a','nested':[1]},{'path':'b','nested':[1]}],
                       [{'path':['unhashable']},{'path':['unhashable']}],
                       ['é','e\u0301'],[None,None],['x','x'],[{},{}]):
            with self.subTest(values=values):
                for unique in (True,False):
                    schema={'uniqueItems':unique}
                    self.assertEqual(WireValidator(schema).is_valid(values),Draft202012Validator(schema).is_valid(values))

    def test_repeated_original_reads_reuse_only_held_path_structure(self):
        r=self.reader();self.begin(r);path=self.path();calls=[]
        class Observed(dict):
            def items(self):calls.append(None);return super().items()
        r._aggregate_dirs=Observed(r._aggregate_dirs)
        self.assertEqual(r.aggregate_bytes(path),self.bodies[path])
        self.assertEqual(r.aggregate_bytes(path),self.bodies[path])
        self.assertLessEqual(len(calls),1,'BODY: repeated reads re-enumerate the entire held directory graph')
        # Cached structure must never cache the actual named/held observation.
        original=self.root/path;other=original.with_name(original.name+'.replacement')
        other.write_bytes(self.bodies[path]);other.replace(original)
        self.refuse('member_custody_changed',lambda:r.aggregate_bytes(path));r.close()

    def test_rebegin_drops_previous_held_path_structure(self):
        import shutil
        r=self.reader();self.begin(r);path=self.path();self.assertTrue(path.startswith('members/'))
        self.assertEqual(r.aggregate_bytes(path),self.bodies[path]);r.close()
        old=(self.root/'members').stat().st_ino
        held=os.open(self.root/'members',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        self.addCleanup(os.close,held)
        shutil.rmtree(self.root/'members');write(self.root,self.bodies)
        self.assertNotEqual((self.root/'members').stat().st_ino,old)
        self.begin(r)
        try:raw=r.aggregate_bytes(path)
        except Refusal as error:self.fail('BODY: stale prior transaction path structure: '+error.reason)
        self.assertEqual(raw,self.bodies[path]);r.aggregate_finish();r.close()

    def test_exact_name_stops_after_actual_entry_and_closes_iterator(self):
        _,c=self.source();r=self.reader();self.begin(r)
        with os.scandir(r.fd) as scan:entry=next(e for e in scan if e.name=='members')
        closed=[]
        class Scan:
            def __enter__(self):return self
            def __exit__(self,*args):closed.append(True)
            def __iter__(self):
                yield entry
                raise AssertionError('BODY: exact entry already observed; scanned unrelated names')
        with patch.object(c.os,'scandir',return_value=Scan()):c.RootReader._aggregate_exact_name(r.fd,'members')
        self.assertEqual(closed,[True]);r.aggregate_finish();r.close()

    def test_each_original_check_bounds_work_to_its_held_ancestry(self):
        # Capacity regression: unrelated branches must not multiply every byte read.
        _,c=self.source();r=self.reader();self.begin(r)
        path=self.path();names=[];real=c.RootReader._aggregate_exact_name
        def checked(parent,name):names.append((parent,name));return real(parent,name)
        with patch.object(c.RootReader,'_aggregate_exact_name',staticmethod(checked)):
            self.assertEqual(r.aggregate_bytes(path),self.bodies[path])
        absolute=len(Path(str(self.root)).parts)-1
        self.assertLessEqual(len(names),2*(absolute+len(Path(path).parts)-1))
        r.aggregate_finish();r.close();self.assertIsNone(r.fd)

    def test_unrelated_held_ancestor_drift_still_refuses_at_terminal_check(self):
        _,c=self.source();r=self.reader();self.begin(r)
        path=self.path();selected=Path(path).parts
        other=next(key.removeprefix('relative:').rstrip('/') for key in r._aggregate_dirs
                   if key.startswith('relative:') and not path.startswith(key.removeprefix('relative:')))
        old=self.root/other;moved=old.with_name(old.name+'-moved')
        self.assertEqual(r.aggregate_bytes(path),self.bodies[path]);old.rename(moved)
        self.refuse('member_custody_changed',r.aggregate_finish)
        r.close();self.assertIsNone(r.fd);self.assertFalse(r._aggregate_files);self.assertFalse(r._aggregate_dirs)

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
        _,c=self.source();alternate=self.root.with_name(self.root.name.upper())
        names={entry.name for entry in os.scandir(self.root.parent)}
        self.assertNotIn(alternate.name,names)
        try:observed=alternate.stat()
        except FileNotFoundError:observed=None
        if observed is not None:
            self.assertEqual((observed.st_dev,observed.st_ino),(self.root.stat().st_dev,self.root.stat().st_ino))
        expected='required_binding_gap' if observed is None else 'member_path_alias'
        print('U3_CASE_OBSERVATION '+__import__('json').dumps(dict(requested=str(alternate),actual=str(self.root),names=sorted(names),old_resolves=observed is not None,dev=self.root.stat().st_dev,inode=self.root.stat().st_ino,reason=expected),sort_keys=True))
        r=c.RootReader.for_aggregate(str(alternate),8*1024**2);self.addCleanup(r.close)
        self.refuse(expected,lambda:self.begin(r))
        self.assertIsNone(r.fd);self.assertFalse(r._aggregate_files);self.assertFalse(r._aggregate_dirs)

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

    def test_actual_file_opens_keep_nonblocking_flag(self):
        _,c=self.source();r=self.reader();real=c.os.open;seen=[]
        def opened(name,flags,**kwargs):
            if name==Path(self.path()).name:self.assertTrue(flags&os.O_NONBLOCK);seen.append(True)
            return real(name,flags,**kwargs)
        with patch.object(c.os,'open',opened):self.begin(r)
        self.assertTrue(seen)

    def test_actual_directory_opens_keep_directory_and_nofollow_flags(self):
        _,c=self.source();r=self.reader();parent=os.open(self.root.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);self.addCleanup(os.close,parent)
        real=c.os.open;seen=[]
        def opened(name,flags,**kwargs):
            self.assertTrue(flags&os.O_DIRECTORY);self.assertTrue(flags&os.O_NOFOLLOW);seen.append(True)
            return real(name,flags,**kwargs)
        with patch.object(c.os,'open',opened):r._aggregate_directory(parent,self.root.name,'fixture-root')
        self.assertEqual(seen,[True])

    def test_unknown_original_request_has_closed_typed_refusal(self):
        r=self.reader();self.begin(r)
        try:r.aggregate_bytes('members/absent')
        except Exception as e:self.assertIsInstance(e,Refusal);self.assertEqual(e.reason,'member_not_in_aggregate')
        else:self.fail('unsigned request returned a value')

    def test_empty_kernel_read_terminates_without_repeated_EOF(self):
        _,c=self.source();r=self.reader();self.begin(r);p=self.path();empty=[];read=c.os.read;clock=c.time.monotonic
        def observed(fd,count):
            raw=read(fd,count)
            if not raw:empty.append(True)
            return raw
        def now():return r._aggregate_deadline+1 if len(empty)>1 else clock()
        with patch.object(c.os,'read',observed),patch.object(c.time,'monotonic',now):
            try:raw=r.aggregate_bytes(p)
            except Exception as e:self.fail('valid held read did not terminate: '+type(e).__name__)
        self.assertEqual(raw,self.bodies[p]);self.assertEqual(empty,[True])

    def test_actual_held_rereads_seek_to_original_start(self):
        _,c=self.source();r=self.reader();self.begin(r);p=self.path();calls=[];real=c.os.lseek
        def seek(fd,offset,whence):calls.append((fd,offset,whence));return real(fd,offset,whence)
        with patch.object(c.os,'lseek',seek):
            try:
                self.assertEqual(r.aggregate_bytes(p),self.bodies[p]);self.assertEqual(r.aggregate_bytes(p),self.bodies[p])
            except Exception as e:self.fail('valid held reread refused: '+type(e).__name__)
        self.assertEqual(calls,[(r._aggregate_files[p][0],0,os.SEEK_SET)]*2)

    def test_relative_foreign_device_refuses_before_file_open(self):
        # Simulated stat observation; no mount/host resource is created.
        _,c=self.source();r=self.reader();name=Path(self.path()).name;real=c.os.stat;opened=[];open_body=c.os.open
        def stat(path,*args,**kwargs):
            info=real(path,*args,**kwargs)
            if path==name and 'dir_fd' in kwargs:
                values=list(info);values[2]+=1;return os.stat_result(values)
            return info
        def open_file(path,*args,**kwargs):
            if path==name:opened.append(True)
            return open_body(path,*args,**kwargs)
        with patch.object(c.os,'stat',stat),patch.object(c.os,'open',open_file):self.refuse('member_path_alias',lambda:self.begin(r))
        self.assertEqual(opened,[])

    def test_relative_foreign_directory_device_refuses_before_open(self):
        # Source-only stat fault injection; no physical mount proof.
        _,c=self.source();r=self.reader();real=c.os.stat;open_body=c.os.open;opened=[]
        def stat(path,*args,**kwargs):
            info=real(path,*args,**kwargs)
            if path=='members' and 'dir_fd' in kwargs:
                values=list(info);values[2]+=1;return os.stat_result(values)
            return info
        def open_dir(path,*args,**kwargs):
            if path=='members':opened.append(True)
            return open_body(path,*args,**kwargs)
        with patch.object(c.os,'stat',stat),patch.object(c.os,'open',open_dir):self.refuse('member_path_alias',lambda:self.begin(r))
        self.assertEqual(opened,[])
