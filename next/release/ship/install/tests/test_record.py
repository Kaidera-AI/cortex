"""S5 install record (day 3): the ONE writer/reader of `<prefix>/install-record.json` (module record.py) and the shell delegation in install.sh. Source-only.
Fixtures: an owned NONSECRET temporary directory only; no installed prefix, engine, image, host, key, signature or custody exists here and test_sign.py / CallerPath are NOT run or imported.
Labels: MODULE = the actual record.py functions; SHELL = the ACTUAL `record_object` function text extracted verbatim from install.sh and run under sh (component-level, NOT the signed
full caller, which is NOT RUN); STATIC = source guards. Fake boundary (named): `owner_uid` is injected where a test cannot chown a real file to a foreign owner."""
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
sys.path.insert(0, str(INSTALL))
import record as REC  # noqa: E402

INSTALL_SH = INSTALL / 'install.sh'
VERIFIER_BLOCK_SHA256 = 'f6aec64849b897a7c30612edde0603fb9b8000db4ce4478fb58d000efd69ea23'   # install.sh from `verify_refuse()` up to `refuse() {` at Bob base 5578ec59
REL = 'test-release-001'
IMG = ('image', 'localhost/cortex-v2-core:test-release-001', 'sha256:' + 'a' * 64)
IMG2 = ('image', 'localhost/cortex-v2-api:test-release-001', 'sha256:' + 'b' * 64)


def legacy_bytes(doc, release, kind, name, ident):
    """The byte-exact output of the retired inline writer of install.sh (copied algorithm), the golden for 'schema unchanged'."""
    doc = dict(doc) if doc else {'release_id': release, 'objects': []}
    doc['release_id'] = release
    doc['objects'] = [o for o in doc['objects'] if o['name'] != name] + [{'kind': kind, 'name': name, 'id': ident}]
    doc['objects'].sort(key=lambda o: o['name'])
    return (json.dumps(doc, sort_keys=True, indent=1) + '\n').encode()


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob('*')) if Path(root).exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-s5-')))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.prefix = self.tmp / 'prefix'
        self.prefix.mkdir(mode=0o700)
        self.path = self.prefix / 'install-record.json'

    def put(self, *obj, release=REL):
        REC.record_object(str(self.prefix), release, *obj)
        self.assertTrue(self.path.exists(), 'the record must exist after a successful record_object')   # a behavioural predicate, so a missing writer fails here and never as a setup error

    def raw(self):
        self.assertTrue(self.path.exists(), 'expected the record file to exist')
        return self.path.read_bytes()

    def lst(self):
        self.assertTrue(os.path.lexists(self.path), 'expected the record file to exist')
        return os.lstat(self.path)

    def outcome(self, call):
        try:
            return ('ok', call())
        except REC.RecordRefusal as refusal:
            return ('refused', refusal.reason)

    def refusal(self, call):
        result = self.outcome(call)
        self.assertEqual(result[0], 'refused', result)
        return result[1]


class ModuleWrite(Base):
    def test_first_write_creates_the_legacy_schema_bytes_private_and_nothing_else(self):
        self.put(*IMG)
        self.assertEqual(self.raw(), legacy_bytes(None, REL, *IMG))
        st = self.lst()
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_nlink, stat.S_ISREG(st.st_mode)), (0o600, os.geteuid(), 1, True))
        self.assertEqual(tree(self.prefix), ['install-record.json'], 'one record file, no temp file, no second store')
        self.assertEqual(stat.S_IMODE(os.lstat(self.prefix).st_mode), 0o700)

    def test_a_second_object_and_a_replace_keep_the_legacy_bytes_and_name_order(self):
        self.put(*IMG2)
        self.put(*IMG)
        first = json.loads(self.raw())
        self.assertEqual(self.raw(), legacy_bytes({'release_id': REL, 'objects': [{'kind': IMG2[0], 'name': IMG2[1], 'id': IMG2[2]}]}, REL, *IMG))
        self.assertEqual([o['name'] for o in first['objects']], sorted([IMG[1], IMG2[1]]))
        self.put('image', IMG[1], 'sha256:' + 'c' * 64)   # replace by name
        got = json.loads(self.raw())
        self.assertEqual({o['name']: o['id'] for o in got['objects']}, {IMG[1]: 'sha256:' + 'c' * 64, IMG2[1]: IMG2[2]})

    def test_the_same_call_again_is_byte_identical_and_leaves_no_temp_file(self):
        self.put(*IMG)
        before = self.raw()
        self.put(*IMG)
        self.assertEqual((self.raw(), tree(self.prefix)), (before, ['install-record.json']))

    def test_read_returns_the_record_and_none_when_there_is_none(self):
        self.assertEqual(REC.read_record(str(self.prefix)), None)
        self.put(*IMG)
        self.put(*IMG2)
        got = REC.read_record(str(self.prefix))
        self.assertEqual(got, {'release_id': REL, 'objects': sorted([{'kind': IMG[0], 'name': IMG[1], 'id': IMG[2]}, {'kind': IMG2[0], 'name': IMG2[1], 'id': IMG2[2]}], key=lambda o: o['name'])})

    def test_forget_removes_only_that_name_and_an_absent_name_writes_nothing(self):
        self.put(*IMG)
        self.put(*IMG2)
        self.assertIs(REC.forget_object(str(self.prefix), IMG[1]), True)
        self.assertEqual([o['name'] for o in REC.read_record(str(self.prefix))['objects']], [IMG2[1]])
        before = (self.raw(), self.lst().st_ino)
        self.assertIs(REC.forget_object(str(self.prefix), 'no-such-object'), False)
        self.assertEqual((self.raw(), self.lst().st_ino), before)
        self.assertIs(REC.forget_object(str(self.prefix), IMG2[1]), True)
        self.assertEqual(REC.read_record(str(self.prefix)), {'release_id': REL, 'objects': []}, 'the release identity stays after the last object')

    def test_forget_with_no_record_is_false_and_creates_nothing(self):
        self.assertIs(REC.forget_object(str(self.prefix), 'x'), False)
        self.assertEqual(tree(self.prefix), [])


class ModuleRefusals(Base):
    def test_another_installations_record_is_never_overwritten(self):
        self.put(*IMG)
        before = self.raw()
        self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), 'other-release', *IMG2)), 'record_foreign_release')
        self.assertEqual((self.raw(), tree(self.prefix)), (before, ['install-record.json']))

    def test_a_malformed_record_is_refused_before_any_effect_by_every_entry(self):
        bad = {'not json': b'{', 'array': b'[]', 'extra key': b'{"release_id":"r","objects":[],"x":1}', 'objects not a list': b'{"release_id":"r","objects":{}}',
               'object keys': b'{"release_id":"r","objects":[{"kind":"image","name":"a"}]}', 'non string id': b'{"release_id":"r","objects":[{"kind":"image","name":"a","id":1}]}',
               'duplicate names': b'{"release_id":"r","objects":[{"kind":"i","name":"a","id":"1"},{"kind":"i","name":"a","id":"2"}]}', 'duplicate json key': b'{"release_id":"r","release_id":"s","objects":[]}',
               'nan': b'{"release_id":"r","objects":[],"n":NaN}', 'empty release': b'{"release_id":"","objects":[]}', 'oversize': b'{"release_id":"r","objects":[],"pad":"' + b'x' * (1 << 20) + b'"}'}
        for name, raw in bad.items():
            with self.subTest(name):
                self.path.write_bytes(raw)
                self.path.chmod(0o600)
                for call in (lambda: REC.read_record(str(self.prefix)), lambda: REC.record_object(str(self.prefix), 'r', *IMG), lambda: REC.forget_object(str(self.prefix), 'a')):
                    self.assertIn(self.refusal(call), ('record_invalid', 'record_unreadable'), name)
                self.assertEqual((self.raw(), tree(self.prefix)), (raw, ['install-record.json']), name)

    def test_symlinks_are_refused_and_nothing_is_written_through_them(self):
        decoy = self.tmp / 'decoy'
        decoy.write_text('decoy')
        decoy.chmod(0o600)
        os.symlink(decoy, self.path)
        self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), REL, *IMG)), 'record_path_unsafe')
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix))), 'record_path_unsafe')
        self.assertEqual((decoy.read_text(), tree(self.prefix)), ('decoy', ['install-record.json']))
        os.unlink(self.path)
        real = self.tmp / 'real-prefix'
        real.mkdir(mode=0o700)
        link = self.tmp / 'link-prefix'
        os.symlink(real, link)
        self.assertEqual(self.refusal(lambda: REC.record_object(str(link), REL, *IMG)), 'record_path_unsafe')
        parent_link = self.tmp / 'parent-link'
        os.symlink(self.tmp, parent_link)
        self.assertEqual(self.refusal(lambda: REC.record_object(str(parent_link / 'prefix'), REL, *IMG)), 'record_path_unsafe')
        self.assertEqual(tree(real), [])

    def test_loose_modes_and_a_foreign_owner_are_refused_and_never_repaired(self):
        self.put(*IMG)
        os.chmod(self.path, 0o644)
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix))), 'record_private')
        self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), REL, *IMG2)), 'record_private')
        self.assertEqual(stat.S_IMODE(self.lst().st_mode), 0o644)
        os.chmod(self.path, 0o600)
        os.chmod(self.prefix, 0o755)
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix))), 'record_private')
        self.assertEqual(stat.S_IMODE(os.lstat(self.prefix).st_mode), 0o755)
        os.chmod(self.prefix, 0o700)
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix), owner_uid=os.geteuid() + 1)), 'record_owner')   # FAKE BOUNDARY: expected owner injected
        self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), REL, *IMG2, owner_uid=os.geteuid() + 1)), 'record_owner')

    def test_the_prefix_owner_and_the_record_owner_are_each_checked_on_their_own(self):   # added after the mutant run; FAKE BOUNDARY: foreign owner injected, a test cannot chown
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix), owner_uid=os.geteuid() + 1)), 'record_owner', 'prefix check alone: there is no record yet')
        self.put(*IMG)
        fd = REC._open_prefix(str(self.prefix), os.geteuid(), must_exist=True)
        self.addCleanup(os.close, fd)
        self.assertEqual(self.outcome(lambda: REC._read(fd, os.geteuid() + 1)), ('refused', 'record_owner'), 'record check alone: the prefix was admitted for the real owner')

    def test_a_failure_after_the_temporary_file_exists_leaves_no_temporary_file_and_the_old_record(self):   # added after the mutant run; FAKE BOUNDARY: the rename is made to fail
        self.put(*IMG)
        before = self.raw()
        with mock.patch.object(REC.os, 'rename', side_effect=OSError('injected')):
            self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), REL, *IMG2)), 'record_write_failed')
        self.assertEqual((self.raw(), tree(self.prefix)), (before, ['install-record.json']))

    def test_a_hard_linked_record_is_refused(self):
        self.put(*IMG)
        os.link(self.path, self.tmp / 'second-name')
        self.assertEqual(self.refusal(lambda: REC.read_record(str(self.prefix))), 'record_private')

    def test_an_io_failure_is_a_typed_non_success_that_keeps_the_old_record_and_leaves_no_temp_file(self):
        self.put(*IMG)
        before = self.raw()
        os.chmod(self.prefix, 0o500)
        self.addCleanup(os.chmod, self.prefix, 0o700)
        self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), REL, *IMG2)), 'record_write_failed')
        self.assertEqual(self.refusal(lambda: REC.forget_object(str(self.prefix), IMG[1])), 'record_write_failed')
        os.chmod(self.prefix, 0o700)
        self.assertEqual((self.raw(), tree(self.prefix)), (before, ['install-record.json']))

    def test_bad_inputs_are_refused_before_any_io(self):
        bad = ({'kind': ''}, {'kind': 'x' * 600}, {'name': ''}, {'name': 'a\nb'}, {'ident': ''}, {'ident': 'a\x00b'}, {'ident': 7}, {'release_id': ''}, {'release_id': None})
        for override in bad:
            with self.subTest(override):
                args = dict(release_id=REL, kind=IMG[0], name=IMG[1], ident=IMG[2])
                args.update(override)
                self.assertEqual(self.refusal(lambda: REC.record_object(str(self.prefix), args['release_id'], args['kind'], args['name'], args['ident'])), 'record_input')
                self.assertEqual(tree(self.prefix), [])
        for prefix in ('relative/prefix', str(self.tmp) + '/../prefix', str(self.tmp / 'missing')):
            self.assertIn(self.refusal(lambda: REC.record_object(prefix, REL, *IMG)), ('record_input', 'record_path_unsafe'), prefix)


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-I', '-B', str(INSTALL / 'record.py'), *args], capture_output=True, text=True)

    def test_the_cli_records_reads_forgets_and_refuses_with_one_typed_reason(self):
        done = self.run_cli('record', '--prefix', str(self.prefix), '--release-id', REL, '--kind', IMG[0], '--name', IMG[1], '--id', IMG[2])
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(self.raw(), legacy_bytes(None, REL, *IMG))
        read = self.run_cli('read', '--prefix', str(self.prefix))
        self.assertEqual((read.returncode, json.loads(read.stdout)['release_id']), (0, REL))
        foreign = self.run_cli('record', '--prefix', str(self.prefix), '--release-id', 'other', '--kind', 'image', '--name', 'n', '--id', 'i')
        self.assertEqual((foreign.returncode, json.loads(foreign.stderr)), (2, {'reason': 'record_foreign_release', 'unit': 'record'}))
        gone = self.run_cli('forget', '--prefix', str(self.prefix), '--name', IMG[1])
        self.assertEqual(gone.returncode, 0)
        self.assertEqual(REC.read_record(str(self.prefix))['objects'], [])


def shell_function():
    text = INSTALL_SH.read_text()
    start = text.index('record_object() {')
    return text[start:text.index('\n}\n', start) + 3]


class Shell(Base):
    """SHELL: the actual function text from install.sh, run under sh. NOT the signed full caller."""

    def call(self, home, release, *args, prefix=None):
        script = f'set -u; HERE="$1"; prefix="$2"; release_id="$3"; {shell_function()}\nshift 3; record_object "$@"'
        return subprocess.run(['sh', '-c', script, 'sh', str(home), str(prefix or self.prefix), release, *args], capture_output=True, text=True)

    def test_the_shell_function_delegates_to_the_record_module_with_exact_arguments(self):
        spy_home = self.tmp / 'spy'
        spy_home.mkdir()
        log = self.tmp / 'spy.log'
        (spy_home / 'record.py').write_text(f"import json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        res = self.call(spy_home, REL, *IMG)
        self.assertEqual((res.returncode, res.stderr), (0, ''))
        self.assertTrue(log.exists(), 'the shell function must call record.py')
        self.assertEqual([json.loads(l) for l in log.read_text().splitlines()],
                         [['record', '--prefix', str(self.prefix), '--release-id', REL, '--kind', IMG[0], '--name', IMG[1], '--id', IMG[2]]])

    def test_the_shell_function_with_the_real_module_writes_exactly_what_the_module_writes(self):
        res = self.call(INSTALL, REL, *IMG)
        self.assertEqual((res.returncode, res.stderr), (0, ''))
        self.assertEqual(self.raw(), legacy_bytes(None, REL, *IMG))
        got = REC.read_record(str(self.prefix))
        self.assertIsNotNone(got, 'the module must read what the shell wrote')
        self.assertEqual(got['objects'][0]['id'], IMG[2])

    def test_a_foreign_record_propagates_the_typed_refusal_and_the_shell_exits_nonzero(self):
        self.put(*IMG)
        before = self.raw()
        res = self.call(INSTALL, 'other-release', *IMG2)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(json.loads(res.stderr.strip().splitlines()[-1])['reason'], 'record_foreign_release')
        self.assertEqual(self.raw(), before)

    def test_the_shell_creates_a_missing_prefix_private_before_delegating(self):
        fresh = self.tmp / 'fresh-prefix'
        res = self.call(INSTALL, REL, *IMG, prefix=fresh)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((stat.S_IMODE(os.lstat(fresh).st_mode), tree(fresh)), (0o700, ['install-record.json']))


class Static(unittest.TestCase):
    def test_install_sh_has_no_second_embedded_writer_or_parser(self):
        text = INSTALL_SH.read_text()
        function = shell_function()
        self.assertEqual(function.count('record.py'), 1)
        for banned in ('<<', 'json.dump', 'json.load', 'os.replace', "python3 -I -" + " ", 'open('):
            self.assertNotIn(banned, function, banned)
        self.assertEqual(len(re.findall(r'install-record\.json', text)), 0, 'the record file name lives only in record.py')

    def test_the_verifier_bytes_of_install_sh_are_unchanged_from_the_base(self):
        text = INSTALL_SH.read_text()
        block = text[text.index('verify_refuse()'):text.index('refuse() {')]
        self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), VERIFIER_BLOCK_SHA256)

    def test_the_record_module_has_an_explicit_standard_library_only_closure_and_no_channel_of_its_own(self):
        tree_ = ast.parse((INSTALL / 'record.py').read_text())
        imported = {a.name.split('.')[0] for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split('.')[0] for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
        self.assertLessEqual(imported, {'errno', 'fcntl', 'json', 'os', 'stat', 'sys', 'uuid', 'pathlib'})
        code = re.sub(r'"""(.|\n)*?"""', '', (INSTALL / 'record.py').read_text())
        for banned in (r'os\.environ', r'subprocess', r'socket', r'urllib', r'\bprint\((?!json)', r'cortex_core', r'importlib'):
            self.assertIsNone(re.search(banned, code), banned)


if __name__ == '__main__':
    unittest.main()
