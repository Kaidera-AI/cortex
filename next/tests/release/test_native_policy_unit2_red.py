"""Frozen unit-2 admission assertions; inert OCI bytes, never native credit.

Tests call R01's real receipt verifier and artifact inspector. The local producer
only constructs synthetic bytes/closure; it does not implement an admission rule.
Expected refusals are assertions in test bodies, never fixture setup failures.
"""
import copy
import importlib.util
import io
import marshal
import struct
import sys
import tarfile
import unittest

from fixture import build_input, canonical
from receipt_fixture import receipt_fixture, sha, tar
import test_deciding_controls as deciding
from test_unit1_red import product


def layer(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for row in entries:
            info = tarfile.TarInfo(row['path'])
            info.uid = row.get('uid', 1001)
            info.gid = row.get('gid', 1001)
            info.mtime = row.get('mtime', 0)
            kind = row.get('kind', 'regular')
            info.mode = row.get('mode', {'regular':420, 'directory':493, 'symlink':511}[kind])
            body = row.get('body', b'')
            if kind == 'directory': info.type = tarfile.DIRTYPE
            elif kind == 'symlink':
                info.type = tarfile.SYMTYPE
                info.linkname = row['target']
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return stream.getvalue()


def inspect(entries, *, policy=None, runtime=None):
    api = product('artifacts')
    value, _ = build_input()
    raw_layer = layer(entries)
    config = canonical({'os':'linux', 'architecture':'amd64', 'config':runtime or {},
                        'rootfs':{'type':'layers', 'diff_ids':['sha256:'+sha(raw_layer)]}})
    def desc(media, raw):
        return {'mediaType':media, 'digest':'sha256:'+sha(raw), 'size':len(raw)}
    manifest = canonical({'schemaVersion':2, 'mediaType':api.MANIFEST,
                          'config':desc(api.CONFIG, config), 'layers':[desc(api.LAYER, raw_layer)]})
    index = canonical({'schemaVersion':2, 'mediaType':api.INDEX,
                       'manifests':[dict(desc(api.MANIFEST, manifest), platform={'os':'linux','architecture':'amd64'})]})
    blobs = {sha(raw):raw for raw in (manifest, config, raw_layer)}
    archive = tar({'oci-layout':canonical({'imageLayoutVersion':'1.0.0'}), 'index.json':index,
                   **{'blobs/sha256/'+key:raw for key,raw in blobs.items()}})
    raw_outputs = {'archive':('application/x-tar',archive), 'index':(api.INDEX,index),
                   'manifest':(api.MANIFEST,manifest), 'config':(api.CONFIG,config),
                   'blob.'+sha(raw_layer):('application/octet-stream',raw_layer),
                   'sbom':('application/json',b'{}'), 'provenance':('application/json',b'{}')}
    records = [dict(role=role,path=role,media_type=media,bytes=len(raw),sha256=sha(raw))
               for role,(media,raw) in raw_outputs.items()]
    oci = dict(index=sha(index),manifest=sha(manifest),config=sha(config),blobs=sorted(blobs),platform='linux/amd64')
    return api.inspect_oci(archive, records, {k:r for k,(_,r) in raw_outputs.items()}, oci, value,
                           module_policy=policy or module_policy())


def module_policy(**changes):
    result = {'oci_config':{}, 'history':[],
              'layer_policy':{'allow_replacements':False,'allow_directories':True,'symlinks':{}}}
    result.update(changes)
    return result


REGULAR = {'path':'app/module.py','body':b'pass\n'}
DIRECTORY = {'path':'app','kind':'directory'}


def pyc_case(source=b'x = 1\n', *, code_edit=None, optimization=0):
    value, _ = build_input()
    value['builder_requirements']['toolchain']['version'] = sys.version.split()[0]
    value['determinism']['python']['optimization'] = optimization
    code = compile(source, '/app/module.py', 'exec', dont_inherit=True, optimize=optimization)
    if code_edit: code = code_edit(code)
    raw = importlib.util.MAGIC_NUMBER + struct.pack('<I',3) + importlib.util.source_hash(source) + marshal.dumps(code)
    return value, source, raw


class NativePolicyUnit2(unittest.TestCase):
    def assert_refused(self, call):
        with self.assertRaises(ValueError): call()

    def test_original_receipt_control_still_verifies_fixture_only(self):
        verified = deciding.DecidingControls().verify(receipt_fixture())
        self.assertFalse(verified.native_qualified)

    def test_reviewed_nonroot_entrypoint_labels_receipt_positive(self):
        runtime = {'User':'1001:1001','Entrypoint':['python3','/app/module.py'],
                   'Labels':{'org.kaidera.module':'api'}}
        verified = deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime))
        self.assertFalse(verified.native_qualified)

    def test_root_user_cannot_be_approved_by_descriptor(self):
        runtime = {'User':'0:0','Entrypoint':['python3','/app/module.py']}
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime)))

    def test_shell_entrypoint_cannot_be_approved_by_descriptor(self):
        runtime = {'User':'1001:1001','Entrypoint':['/bin/sh','-c','python3 /app/module.py']}
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime)))

    def test_secret_environment_cannot_be_approved_by_descriptor(self):
        runtime = {'User':'1001:1001','Env':['CORTEX_ADMIN_TOKEN=synthetic-test-value']}
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime)))

    def test_unknown_config_key_cannot_be_approved_by_descriptor(self):
        runtime = {'User':'1001:1001','UnknownRuntimeKnob':True}
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime)))

    def test_retained_labels_are_value_bound(self):
        approved = {'User':'1001:1001','Labels':{'org.kaidera.module':'api'}}
        changed = copy.deepcopy(approved); changed['Labels']['org.kaidera.module']='core'
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=changed,approved_runtime_config=approved)))

    def test_explicit_directory_inventory_is_measured(self):
        _, inventory = inspect([DIRECTORY,REGULAR])
        self.assertEqual(inventory[0]['kind'],'directory')
        self.assertEqual(inventory[0]['mode'],493)

    def test_undeclared_directory_is_not_admitted_by_blanket_boolean(self):
        p=module_policy(); p['layer_policy']['directories']={'app':{'mode':493,'uid':1001,'gid':1001,'mtime':0}}
        self.assert_refused(lambda:inspect([DIRECTORY,REGULAR,{'path':'extra','kind':'directory'}],policy=p))

    def test_directory_epoch_drift_refuses(self):
        self.assert_refused(lambda:inspect([dict(DIRECTORY,mtime=1),REGULAR]))

    def test_setuid_regular_refuses(self):
        self.assert_refused(lambda:inspect([dict(REGULAR,mode=0o4755)]))

    def test_declared_safe_runtime_link_inventory_positive(self):
        p=module_policy(); p['layer_policy']['symlinks']={'app/current.py':'module.py'}
        _, inventory=inspect([DIRECTORY,REGULAR,{'path':'app/current.py','kind':'symlink','target':'module.py'}],policy=p)
        row=next(r for r in inventory if r['path']=='app/current.py')
        self.assertEqual(row['link_target'],'module.py')
        self.assertEqual(row['kind'],'symlink')

    def test_link_changed_target_refuses(self):
        p=module_policy(); p['layer_policy']['symlinks']={'app/current.py':'module.py'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/current.py','kind':'symlink','target':'other.py'}],policy=p))

    def test_link_escape_refuses(self):
        p=module_policy(); p['layer_policy']['symlinks']={'app/current.py':'../../outside'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/current.py','kind':'symlink','target':'../../outside'}],policy=p))

    def test_link_dangling_refuses(self):
        p=module_policy(); p['layer_policy']['symlinks']={'app/current.py':'missing.py'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/current.py','kind':'symlink','target':'missing.py'}],policy=p))

    def test_file_under_symlink_parent_refuses(self):
        p=module_policy(); p['layer_policy']['symlinks']={'app/alias':'module.py'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/alias','kind':'symlink','target':'module.py'},
                                          {'path':'app/alias/hidden.py','body':b'pass\n'}],policy=p))

    def check_cache(self, value, source, raw):
        return product('artifacts')._check_pyc('app/module.pyc',raw,{'app/module.py':source},value)

    def test_checked_hash_bytecode_positive(self):
        self.check_cache(*pyc_case())

    def test_unchecked_hash_refuses(self):
        value,source,raw=pyc_case();raw=raw[:4]+struct.pack('<I',1)+raw[8:]
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_timestamp_mode_refuses(self):
        value,source,raw=pyc_case();raw=raw[:4]+struct.pack('<I',0)+raw[8:]
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_wrong_source_hash_refuses(self):
        value,source,raw=pyc_case();raw=raw[:8]+b'wronghash'+raw[16:]
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_trailing_marshal_refuses(self):
        value,source,raw=pyc_case()
        self.assert_refused(lambda:self.check_cache(value,source,raw+b'foreign'))

    def test_wrong_final_filename_refuses(self):
        value,source,raw=pyc_case(code_edit=lambda c:c.replace(co_filename='/tmp/builder/module.py'))
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_typed_constant_true_is_not_integer_one(self):
        value,source,raw=pyc_case(code_edit=lambda c:c.replace(co_consts=tuple(True if type(x) is int and x==1 else x for x in c.co_consts)))
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_float_signed_zero_bit_pattern_is_bound(self):
        value,source,raw=pyc_case(b'x = 0.0\n',code_edit=lambda c:c.replace(co_consts=tuple(-0.0 if type(x) is float and x==0 else x for x in c.co_consts)))
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_line_table_is_bound(self):
        value,source,raw=pyc_case(code_edit=lambda c:c.replace(co_linetable=b''))
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_empty_user_cannot_be_approved_by_descriptor(self):
        runtime = {'User':'','Entrypoint':['python3','/app/module.py']}
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(runtime_config=runtime,approved_runtime_config=runtime)))

    def test_orphan_bytecode_refuses(self):
        value,source,raw=pyc_case()
        self.assert_refused(lambda:product('artifacts')._check_pyc('app/module.pyc',raw,{},value))

    def test_unknown_pyc_flag_bits_refuse(self):
        value,source,raw=pyc_case();raw=raw[:4]+struct.pack('<I',7)+raw[8:]
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_nonpython_policy_cannot_hide_cache(self):
        value,source,raw=pyc_case();value['determinism']['python']={'applicable':False,'reason':'non-python'}
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_wrong_compiler_magic_refuses(self):
        value,source,raw=pyc_case();raw=b'bad!'+raw[4:]
        self.assert_refused(lambda:self.check_cache(value,source,raw))

    def test_link_cycle_refuses(self):
        p=module_policy();p['layer_policy']['symlinks']={'app/a':'b','app/b':'a'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/a','kind':'symlink','target':'b'},
                                          {'path':'app/b','kind':'symlink','target':'a'}],policy=p))

    def test_absolute_link_target_refuses(self):
        p=module_policy();p['layer_policy']['symlinks']={'app/a':'/app/module.py'}
        self.assert_refused(lambda:inspect([REGULAR,{'path':'app/a','kind':'symlink','target':'/app/module.py'}],policy=p))

    def test_generated_log_refuses(self):
        self.assert_refused(lambda:inspect([REGULAR,{'path':'var/log/build.log','body':b'generated'}]))

    def test_existing_raw_equality_still_refuses(self):
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(unequal_archive=True)))

    def test_existing_cleanup_still_refuses(self):
        self.assert_refused(lambda:deciding.DecidingControls().verify(receipt_fixture(),edit=lambda r:r['cleanup'].update(complete=False)))


if __name__=='__main__': unittest.main(verbosity=2)
