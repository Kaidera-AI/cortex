"""J1 project unlock key (row feb9dc8b): the installer-side private opener for ONE option-C project root, tested at the ACTUAL project_key module and, for the final join, Core's ACTUAL
runtime.custody_fs.read_unlock. Stdlib only, no entropy, no signing key: every key is a fixed NONSECRET constant derived from a fixture label and written into an owned private temporary
directory on the internal disk (never the noowners SSD). Deciders: test_persistent_unlock_identity_and_unsafe_key_refusal and test_project_handover_one_session_private_records.

BOUNDARIES NAMED. Fixtures are NOT native isolation or reboot credit. Not run: a second OS user, a real ACL, a real ownership-ignoring mount, Linux, an admitted host identity, a process
restart or reboot. The owner/uid checks use an injected effective uid (a test cannot chown); the ACL and mount probes use named doubles plus real negative controls on a clean temp file.
The key-id oracle (_kid) is written here from the contract text, independently of project_key.

RED CONTRACT: every refusal is a typed KeyRefusal with an exact reason, leaves no descriptor, no output, no repair (mode, owner, existence unchanged), and carries no path or key byte in its
text. At the interface commit every behaviour test FAILS by assertion (the module refuses install_not_wired); the static controls and the pure-oracle checks pass."""
import ast
import base64
import contextlib
import copy
import fcntl
import hashlib
import io
import logging
import os
import pickle
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
SRC = INSTALL.parent.parent.parent / 'src'
sys.path.insert(0, str(INSTALL))
import project_key as PK  # noqa: E402

PROJECT = '11111111-2222-4333-8444-555555555555'
OTHER_PROJECT = '66666666-7777-4888-9999-000000000000'
ACCOUNT = pwd.getpwuid(os.geteuid()).pw_name
KEY_A = hashlib.sha256(b'j1-fixture-project-a').digest()
KEY_B = hashlib.sha256(b'j1-fixture-project-b').digest()
KEY_INSTALL = hashlib.sha256(b'j1-fixture-installation').digest()


def _kid(key):
    return 'pk1-' + hashlib.sha256(b'cortex-project-key-id-v1\x00' + key).hexdigest()[:32]


KID_A, KID_B, KID_INSTALL = _kid(KEY_A), _kid(KEY_B), _kid(KEY_INSTALL)


def _fds():
    return sorted(int(n) for n in os.listdir('/dev/fd') if n.isdigit())


def _needles(*keys):
    out = []
    for key in keys:
        out += [key.hex(), key.hex().upper(), base64.b64encode(key).decode(), base64.urlsafe_b64encode(key).decode(), repr(key), str(list(key))]
    return out


class Harness(unittest.TestCase):
    """Builds owned private trees; every call returns an outcome tuple, so a missing behaviour is an assertion failure and never a setup error."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix='j1key-'))
        self.addCleanup(self._rm)
        self.opened = []
        self.addCleanup(self._close_all)
        self.n = 0

    def _rm(self):
        for root, dirs, files in os.walk(self.base):
            os.chmod(root, 0o700)
        shutil.rmtree(self.base, ignore_errors=True)

    def _close_all(self):
        for fd in self.opened:
            try:
                os.close(fd)
            except OSError:
                pass

    def tree(self, key=KEY_A, kid=None, mode=0o400, dirmode=0o700, project=PROJECT, identity=None, parent=None):
        self.n += 1
        kid = kid or _kid(key)
        parent = parent or self.base
        folder = os.path.join(parent, 'p%d' % self.n)
        os.mkdir(folder, 0o700)
        path = os.path.join(folder, kid + '.key')
        if key is not None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        os.chmod(folder, dirmode)
        ref = dict(kind='project-key-file', path=path, key_id=kid, identity=identity or ACCOUNT, project_id=project)
        return ref, folder, path

    def run_call(self, fn, *a, **k):
        out, err, before = io.StringIO(), io.StringIO(), _fds()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                value = fn(*a, **k)
                result = ('ok', value)
            except PK.KeyRefusal as e:
                result = ('refused', e.reason, str(e), repr(e))
            except BaseException as e:  # noqa: BLE001 - classified, never hidden
                result = ('raised', type(e).__name__, str(e)[:200])
        self.printed = out.getvalue() + err.getvalue()
        after = _fds()
        if result[0] == 'ok' and type(result[1]) is int:
            self.opened.append(result[1])
            after = [f for f in after if f != result[1]]
        self.leaked = [f for f in after if f not in before]
        return result

    def open_it(self, ref, installation=KID_INSTALL, project=PROJECT):
        return self.run_call(PK.open_unlock, ref, project_id=project, installation_key_id=installation)

    def assert_clean_refusal(self, result, reason, *secrets):
        self.assertEqual(result[:2], ('refused', reason), result)
        self.assertEqual(self.leaked, [], 'descriptor leaked on refusal')
        self.assertEqual(self.printed, '', 'refusal wrote output')
        text = ' '.join(str(p) for p in result[2:]) + self.printed
        for needle in list(secrets) + _needles(KEY_A, KEY_B, KEY_INSTALL):
            self.assertNotIn(needle, text)


class Reference(Harness):
    def test_valid_reference_is_returned_unchanged(self):
        ref, _, _ = self.tree()
        r = self.run_call(PK.validate_reference, ref, project_id=PROJECT)
        self.assertEqual(r[0], 'ok')
        self.assertEqual(r[1], ref)

    def test_reference_shape_refusals(self):
        ref, _, path = self.tree()
        base = dict(ref)
        cases = {
            'missing key': {k: v for k, v in base.items() if k != 'identity'},
            'extra key': {**base, 'extra': 'x'},
            'wrong kind': {**base, 'kind': 'inline-key'},
            'relative path': {**base, 'path': 'keys/' + KID_A + '.key'},
            'dotdot path': {**base, 'path': os.path.dirname(path) + '/../' + KID_A + '.key'},
            'double slash': {**base, 'path': '/' + path},
            'trailing slash': {**base, 'path': path + '/'},
            'name is not key id': {**base, 'path': os.path.join(os.path.dirname(path), 'other.key')},
            'key id bad characters': {**base, 'key_id': 'pk1 bad!'},
            'key id too long': {**base, 'key_id': 'k' * 65},
            'key id empty': {**base, 'key_id': ''},
            'identity empty': {**base, 'identity': ''},
            'identity with slash': {**base, 'identity': 'a/b'},
            'project id not uuid': {**base, 'project_id': 'not-a-uuid'},
            'path not str': {**base, 'path': b'/x'},
            'bool identity': {**base, 'identity': True},
            'not a dict': [('kind', 'project-key-file')],
            'dict subclass': type('D', (dict,), {})(base),
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assert_clean_refusal(self.run_call(PK.validate_reference, value, project_id=PROJECT), 'unlock_reference_invalid')

    def test_reference_for_another_project_is_foreign(self):
        ref, _, _ = self.tree(project=OTHER_PROJECT)
        self.assert_clean_refusal(self.run_call(PK.validate_reference, ref, project_id=PROJECT), 'unlock_reference_foreign')

    def test_key_id_derivation_matches_the_contract_text(self):
        for key, kid in ((KEY_A, KID_A), (KEY_B, KID_B), (KEY_INSTALL, KID_INSTALL)):
            with self.subTest(kid):
                r = self.run_call(PK.derive_key_id, key)
                self.assertEqual(r, ('ok', kid), r)
        self.assertEqual(len({KID_A, KID_B, KID_INSTALL}), 3)
        self.assertRegex(KID_A, r'\Apk1-[0-9a-f]{32}\Z')
        self.assertRegex(KID_A, r'[A-Za-z0-9_-]{1,64}\Z')

    def test_key_id_derivation_refuses_non_keys(self):
        for label, value in (('31 bytes', KEY_A[:31]), ('33 bytes', KEY_A + b'x'), ('str', 'x' * 32), ('bytearray', bytearray(KEY_A)), ('none', None), ('empty', b'')):
            with self.subTest(label):
                self.assert_clean_refusal(self.run_call(PK.derive_key_id, value), 'unlock_key_unsafe')


class Admission(Harness):
    def test_valid_key_opens_a_fresh_readonly_private_fd(self):
        ref, _, _ = self.tree()
        r = self.open_it(ref)
        self.assertEqual(r[0], 'ok', r)
        fd = r[1]
        self.assertIs(type(fd), int)
        self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
        self.assertFalse(os.get_inheritable(fd))
        self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
        s = os.fstat(fd)
        self.assertTrue(stat.S_ISREG(s.st_mode) and s.st_size == 32 and s.st_nlink == 1 and stat.S_IMODE(s.st_mode) == 0o400)
        self.assertEqual(os.read(fd, 33), KEY_A)
        self.assertEqual(self.printed, '')

    def test_each_call_returns_a_new_independent_descriptor(self):
        ref, _, _ = self.tree()
        a, b = self.open_it(ref), self.open_it(ref)
        self.assertEqual((a[0], b[0]), ('ok', 'ok'))
        self.assertNotEqual(a[1], b[1])
        self.assertEqual(os.read(a[1], 16), KEY_A[:16])
        self.assertEqual(os.read(b[1], 32), KEY_A)

    def test_core_read_unlock_accepts_the_descriptor(self):
        """JOIN with Core's ACTUAL descriptor-only reader (not a double)."""
        sys.path.insert(0, str(SRC))
        try:
            from cortex_core.runtime import custody_fs
        finally:
            sys.path.remove(str(SRC))
        ref, _, _ = self.tree()
        r = self.open_it(ref)
        self.assertEqual(r[0], 'ok', r)
        self.assertEqual(custody_fs.read_unlock(r[1]), KEY_A)

    def test_real_probes_are_negative_on_a_clean_private_file(self):
        ref, _, path = self.tree()
        fd = os.open(path, os.O_RDONLY)
        self.opened.append(fd)
        self.assertEqual(self.run_call(PK._acl_present, fd), ('ok', False))
        self.assertEqual(self.run_call(PK._mount_ignores_ownership, fd), ('ok', False))

    def test_darwin_ownership_flag_decider(self):
        self.assertEqual(self.run_call(PK._darwin_flags_ignore_ownership, 0x00200000), ('ok', True))
        self.assertEqual(self.run_call(PK._darwin_flags_ignore_ownership, 0x00200000 | 0x1), ('ok', True))
        self.assertEqual(self.run_call(PK._darwin_flags_ignore_ownership, 0x1 | 0x4000), ('ok', False))
        self.assertEqual(self.run_call(PK._darwin_flags_ignore_ownership, 0), ('ok', False))

    def test_linux_mount_table_decider(self):
        def line(mount_id, devno, fstype, opts='rw,relatime'):
            return '%d 1 %s / /mnt/x %s - %s source rw\n' % (mount_id, devno, opts, fstype)
        for fstype, expect in (('ext4', True), ('xfs', True), ('btrfs', True), ('tmpfs', True), ('vfat', False), ('exfat', False), ('ntfs3', False), ('fuseblk', False), ('9p', False), ('nfs4', False), ('cifs', False)):
            with self.subTest(fstype):
                text = line(10, '8:1', 'ext4') + line(41, '8:17', fstype)
                self.assertEqual(self.run_call(PK._mount_honours_ownership_linux, text, os.makedev(8, 17)), ('ok', expect))
        with self.subTest('device not in the table'):
            self.assertEqual(self.run_call(PK._mount_honours_ownership_linux, line(10, '8:1', 'ext4'), os.makedev(9, 9))[:2], ('refused', 'unlock_mount_unsafe'))
        with self.subTest('malformed table'):
            self.assertEqual(self.run_call(PK._mount_honours_ownership_linux, 'garbage\n', os.makedev(8, 17))[:2], ('refused', 'unlock_mount_unsafe'))

    def test_stat_deciders_are_pure_and_exact(self):
        from types import SimpleNamespace as St
        uid = 501
        def leaf(**k):
            base = dict(st_mode=stat.S_IFREG | 0o400, st_uid=uid, st_nlink=1, st_size=32)
            base.update(k)
            return St(**base)
        def folder(**k):
            base = dict(st_mode=stat.S_IFDIR | 0o700, st_uid=uid, st_nlink=2, st_size=0)
            base.update(k)
            return St(**base)
        self.assertEqual(self.run_call(PK._leaf_safe, leaf(), uid), ('ok', True))
        for label, st in (('foreign owner', leaf(st_uid=uid + 1)), ('root owner', leaf(st_uid=0)), ('mode 0440', leaf(st_mode=stat.S_IFREG | 0o440)), ('mode 0600', leaf(st_mode=stat.S_IFREG | 0o600)),
                          ('setuid', leaf(st_mode=stat.S_IFREG | 0o4400)), ('two links', leaf(st_nlink=2)), ('size 31', leaf(st_size=31)), ('size 33', leaf(st_size=33)),
                          ('directory', leaf(st_mode=stat.S_IFDIR | 0o400)), ('fifo', leaf(st_mode=stat.S_IFIFO | 0o400)), ('symlink', leaf(st_mode=stat.S_IFLNK | 0o400))):
            with self.subTest('leaf ' + label):
                self.assertEqual(self.run_call(PK._leaf_safe, st, uid), ('ok', False))
        self.assertEqual(self.run_call(PK._dir_safe, folder(), uid, private=True), ('ok', True))
        for label, st in (('foreign owner', folder(st_uid=uid + 1)), ('root owner', folder(st_uid=0)), ('mode 0750', folder(st_mode=stat.S_IFDIR | 0o750)), ('mode 0755', folder(st_mode=stat.S_IFDIR | 0o755)),
                          ('mode 0500', folder(st_mode=stat.S_IFDIR | 0o500)), ('regular file', folder(st_mode=stat.S_IFREG | 0o700))):
            with self.subTest('private dir ' + label):
                self.assertEqual(self.run_call(PK._dir_safe, st, uid, private=True), ('ok', False))
        for label, st, expect in (('own 0755', folder(st_mode=stat.S_IFDIR | 0o755), True), ('root 0755', folder(st_uid=0, st_mode=stat.S_IFDIR | 0o755), True),
                                  ('own 0700', folder(), True), ('root sticky 1777', folder(st_uid=0, st_mode=stat.S_IFDIR | 0o1777), True),
                                  ('own group-writable', folder(st_mode=stat.S_IFDIR | 0o775), False), ('own world-writable', folder(st_mode=stat.S_IFDIR | 0o757), False),
                                  ('other owner 0755', folder(st_uid=uid + 1, st_mode=stat.S_IFDIR | 0o755), False), ('root world-writable no sticky', folder(st_uid=0, st_mode=stat.S_IFDIR | 0o777), False),
                                  ('not a directory', folder(st_mode=stat.S_IFLNK | 0o755), False)):
            with self.subTest('ancestor ' + label):
                self.assertEqual(self.run_call(PK._dir_safe, st, uid, private=False), ('ok', expect))

    def test_persistent_unlock_identity_and_unsafe_key_refusal(self):
        """DECIDER. Each case builds its own tree, must refuse with the EXACT reason before any descriptor leaves, with no output, no repair and no path or key byte in the text."""
        uid = os.geteuid()

        def absent():
            ref, folder, path = self.tree(key=None, kid=KID_A)
            return ref, lambda: self.assertFalse(os.path.lexists(path), 'a refusal created the key')

        def wrong_key():
            ref, folder, path = self.tree(key=KEY_B, kid=KID_A)
            return ref, None

        def swapped_between_projects():
            ref, folder, path = self.tree(key=KEY_B, kid=KID_A, project=PROJECT)
            return ref, None

        def mode(m):
            def make():
                ref, folder, path = self.tree(mode=m)
                return ref, lambda: self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), m, 'a refusal repaired the mode')
            return make

        def size(n):
            def make():
                ref, folder, path = self.tree(key=(KEY_A + KEY_B)[:n], kid=KID_A)
                return ref, None
            return make

        def symlink_leaf():
            ref, folder, path = self.tree()
            real = path + '.real'
            os.rename(path, real)
            os.symlink(real, path)
            return ref, None

        def hard_link():
            ref, folder, path = self.tree()
            os.link(path, path + '.again')
            return ref, None

        def leaf_is_directory():
            ref, folder, path = self.tree(key=None, kid=KID_A)
            os.mkdir(path, 0o700)
            return ref, None

        def leaf_is_fifo():
            ref, folder, path = self.tree(key=None, kid=KID_A)
            os.mkfifo(path, 0o400)
            return ref, None

        def parent_mode(m):
            def make():
                ref, folder, path = self.tree(dirmode=m)
                return ref, lambda: self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), m, 'a refusal repaired the directory mode')
            return make

        def symlink_ancestor():
            ref, folder, path = self.tree()
            link = os.path.join(self.base, 'viaLink')
            os.symlink(folder, link)
            return dict(ref, path=os.path.join(link, os.path.basename(path))), None

        def group_writable_ancestor():
            shared = os.path.join(self.base, 'shared')
            os.mkdir(shared, 0o700)
            os.chmod(shared, 0o770)
            ref, folder, path = self.tree(parent=shared)
            return ref, None

        def world_writable_ancestor():
            shared = os.path.join(self.base, 'open')
            os.mkdir(shared, 0o700)
            os.chmod(shared, 0o777)
            ref, folder, path = self.tree(parent=shared)
            return ref, None

        cases = [
            ('absent key', absent, 'unlock_key_absent', {}),
            ('wrong key for the reference', wrong_key, 'unlock_key_mismatch', {}),
            ('key swapped from another project', swapped_between_projects, 'unlock_key_mismatch', {}),
            ('mode 0440', mode(0o440), 'unlock_key_unsafe', {}),
            ('mode 0600', mode(0o600), 'unlock_key_unsafe', {}),
            ('mode 0444', mode(0o444), 'unlock_key_unsafe', {}),
            ('mode 0500', mode(0o500), 'unlock_key_unsafe', {}),
            ('size 31', size(31), 'unlock_key_unsafe', {}),
            ('size 33', size(33), 'unlock_key_unsafe', {}),
            ('size 0', size(0), 'unlock_key_unsafe', {}),
            ('symlink leaf', symlink_leaf, 'unlock_key_unsafe', {}),
            ('hard link', hard_link, 'unlock_key_unsafe', {}),
            ('leaf is a directory', leaf_is_directory, 'unlock_key_unsafe', {}),
            ('leaf is a fifo', leaf_is_fifo, 'unlock_key_unsafe', {}),
            ('parent mode 0755', parent_mode(0o755), 'unlock_path_unsafe', {}),
            ('parent mode 0750', parent_mode(0o750), 'unlock_path_unsafe', {}),
            ('parent mode 0777', parent_mode(0o777), 'unlock_path_unsafe', {}),
            ('symlink ancestor', symlink_ancestor, 'unlock_path_unsafe', {}),
            ('group-writable ancestor', group_writable_ancestor, 'unlock_path_unsafe', {}),
            ('world-writable ancestor without sticky', world_writable_ancestor, 'unlock_path_unsafe', {}),
            ('installation key selected by a project reference', lambda: (self.tree(key=KEY_INSTALL)[0], None), 'unlock_key_installation', {}),
            ('foreign execution identity', lambda: (self.tree(identity='someone-else')[0], None), 'unlock_identity_mismatch', {}),
            ('foreign owner of the directory chain (injected uid)', lambda: (self.tree()[0], None), 'unlock_path_unsafe', {'_euid': lambda: uid + 1, '_account': lambda u: ACCOUNT}),
            ('privileged identity (injected uid 0)', lambda: (self.tree(identity='root')[0], None), 'unlock_identity_privileged', {'_euid': lambda: 0, '_account': lambda u: 'root'}),
            ('ACL present', lambda: (self.tree()[0], None), 'unlock_acl_present', {'_acl_present': lambda fd: True}),
            ('ownership-ignoring mount', lambda: (self.tree()[0], None), 'unlock_mount_unsafe', {'_mount_ignores_ownership': lambda fd: True}),
            ('name replaced after the read', lambda: (self.tree()[0], None), 'unlock_key_unsafe', {'_after_read': 'swap'}),
        ]
        for label, build, reason, patches in cases:
            with self.subTest(label):
                ref, check = build()
                patchers = []
                for name, value in patches.items():
                    if value == 'swap':
                        path = ref['path']

                        def swap(path=path):
                            # the replacement is created while the old file still exists, so it can never reuse the inode
                            fd = os.open(path + '.new', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            os.write(fd, KEY_A)
                            os.fchmod(fd, 0o400)
                            os.close(fd)
                            os.replace(path + '.new', path)
                        value = swap
                    patchers.append(mock.patch.object(PK, name, value))
                with contextlib.ExitStack() as stack:
                    for p in patchers:
                        stack.enter_context(p)
                    result = self.open_it(ref)
                self.assert_clean_refusal(result, reason, ref['path'], os.path.dirname(ref['path']))
                if check:
                    check()

    def test_directory_swapped_after_the_read_is_refused(self):
        """The key's parent directory is replaced (same name, new inode, same key bytes) between the read and the return: the chain is re-checked and the call refuses."""
        ref, folder, path = self.tree()

        def swap():
            os.rename(folder, folder + '.old')
            os.mkdir(folder, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, KEY_A)
            os.fchmod(fd, 0o400)
            os.close(fd)
        with mock.patch.object(PK, '_after_read', swap):
            result = self.open_it(ref)
        self.assert_clean_refusal(result, 'unlock_path_unsafe', path, folder)

    def test_sticky_world_writable_ancestor_is_admitted_like_a_tmp_directory(self):
        shared = os.path.join(self.base, 'tmp-like')
        os.mkdir(shared, 0o700)
        os.chmod(shared, 0o1777)
        ref, _, _ = self.tree(parent=shared)
        r = self.open_it(ref)
        self.assertEqual(r[0], 'ok', r)

    def test_reference_for_another_project_refuses_at_open(self):
        ref, _, _ = self.tree(project=OTHER_PROJECT)
        self.assert_clean_refusal(self.open_it(ref), 'unlock_reference_foreign')


class Session(Harness):
    def make(self, ref, deadline=1000.0, installation=KID_INSTALL):
        return self.run_call(PK.UnlockSession, ref, project_id=PROJECT, installation_key_id=installation, deadline=deadline)

    def test_project_handover_one_session_private_records(self):
        """DECIDER. One lead session admits the key once, then hands a NEW private descriptor to each finite operation. It refuses wrong or missing key, a foreign project, the
        installation key, an unsafe filesystem, another process or a copy (the ordinary worker), a closed session and an expired original deadline, before any effect; nothing printed or
        raised carries a path or a key byte."""
        with mock.patch.object(PK, '_now', lambda: 10.0):
            for label, build, reason in (
                    ('missing key', lambda: self.tree(key=None, kid=KID_A)[0], 'unlock_key_absent'),
                    ('wrong key', lambda: self.tree(key=KEY_B, kid=KID_A)[0], 'unlock_key_mismatch'),
                    ('foreign project reference', lambda: self.tree(project=OTHER_PROJECT)[0], 'unlock_reference_foreign'),
                    ('installation key', lambda: self.tree(key=KEY_INSTALL)[0], 'unlock_key_installation'),
                    ('unsafe parent directory', lambda: self.tree(dirmode=0o755)[0], 'unlock_path_unsafe'),
                    ('unsafe key mode', lambda: self.tree(mode=0o640)[0], 'unlock_key_unsafe'),
            ):
                with self.subTest('construct: ' + label):
                    ref = build()
                    self.assert_clean_refusal(self.make(ref), reason, ref['path'])
            for label, deadline in (('bool', True), ('nan', float('nan')), ('inf', float('inf')), ('string', '9'), ('none', None)):
                with self.subTest('deadline ' + label):
                    ref, _, _ = self.tree()
                    self.assert_clean_refusal(self.make(ref, deadline=deadline), 'unlock_deadline_invalid')
            with self.subTest('already expired at construction'):
                ref, _, _ = self.tree()
                self.assert_clean_refusal(self.make(ref, deadline=10.0), 'unlock_session_expired')

            ref, folder, path = self.tree()
            made = self.make(ref, deadline=20.0)
            self.assertEqual(made[0], 'ok', made)
            session = made[1]
            with self.subTest('each finite operation gets a NEW descriptor'):
                a, b = self.run_call(session.fresh_fd), self.run_call(session.fresh_fd)
                self.assertEqual((a[0], b[0]), ('ok', 'ok'))
                self.assertNotEqual(a[1], b[1])
                self.assertEqual(os.read(a[1], 32), KEY_A)
                self.assertEqual(os.read(b[1], 32), KEY_A)
            with self.subTest('the descriptor never reaches a child process'):
                got = self.run_call(session.fresh_fd)
                done = subprocess.run([sys.executable, '-I', '-c', 'import os,sys\ntry:\n os.fstat(int(sys.argv[1]))\nexcept OSError:\n sys.exit(0)\nsys.exit(7)', str(got[1])], close_fds=False)
                self.assertEqual(done.returncode, 0, 'a child inherited the unlock descriptor')
            with self.subTest('repr and str carry no path or key'):
                text = repr(session) + str(session)
                for needle in [path, folder, KID_A] + _needles(KEY_A):
                    self.assertNotIn(needle, text)
            with self.subTest('copy, deepcopy and pickle refuse'):
                for fn in (copy.copy, copy.deepcopy, pickle.dumps):
                    self.assert_clean_refusal(self.run_call(fn, session), 'unlock_session_copy')
            with self.subTest('another process (the ordinary worker) is refused'):
                with mock.patch.object(PK, '_pid', lambda: os.getpid() + 1):
                    self.assert_clean_refusal(self.run_call(session.fresh_fd), 'unlock_session_foreign_process')
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:
                    try:
                        os.close(r)
                        try:
                            session.fresh_fd()
                            os.write(w, b'ok')
                        except PK.KeyRefusal as e:
                            os.write(w, e.reason.encode())
                        except BaseException as e:  # noqa: BLE001
                            os.write(w, type(e).__name__.encode())
                    finally:
                        os._exit(0)
                os.close(w)
                reported = os.read(r, 200)
                os.close(r)
                os.waitpid(pid, 0)
                self.assertEqual(reported, b'unlock_session_foreign_process')
            with self.subTest('the key swapped after the session began is refused at the next operation'):
                os.unlink(path)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, KEY_B)
                os.fchmod(fd, 0o400)
                os.close(fd)
                self.assert_clean_refusal(self.run_call(session.fresh_fd), 'unlock_key_mismatch', path)
                os.unlink(path)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, KEY_A)
                os.fchmod(fd, 0o400)
                os.close(fd)
            with self.subTest('the original deadline expires the session'):
                self.assertEqual(self.run_call(session.fresh_fd)[0], 'ok')
                with mock.patch.object(PK, '_now', lambda: 20.0):
                    self.assert_clean_refusal(self.run_call(session.fresh_fd), 'unlock_session_expired')
                self.assertEqual(self.run_call(session.fresh_fd)[0], 'ok', 'expiry was sticky only by the clock, not by state')
            with self.subTest('close ends the session for good and is idempotent'):
                self.assertEqual(self.run_call(session.close), ('ok', None))
                self.assertEqual(self.run_call(session.close), ('ok', None))
                self.assert_clean_refusal(self.run_call(session.fresh_fd), 'unlock_session_closed')

    def test_a_sequence_of_operations_prints_and_logs_no_preimage(self):
        records = []

        class Catch(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Catch()
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        ref, folder, path = self.tree()
        with mock.patch.object(PK, '_now', lambda: 1.0):
            made = self.make(ref)
            self.assertEqual(made[0], 'ok', made)
            for _ in range(3):
                got = self.run_call(made[1].fresh_fd)
                self.assertEqual(got[0], 'ok')
            self.run_call(made[1].close)
        self.assertEqual(self.printed, '')
        text = ' '.join(records)
        for needle in _needles(KEY_A) + [path, folder]:
            self.assertNotIn(needle, text)


class Static(unittest.TestCase):
    """Source-level controls on project_key.py (these also hold for the skeleton)."""

    def setUp(self):
        self.text = (INSTALL / 'project_key.py').read_text()
        self.tree = ast.parse(self.text)

    def test_stdlib_only_imports(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])
        self.assertLessEqual(names, {'ctypes', 'errno', 'fcntl', 'hashlib', 'hmac', 'os', 'pwd', 're', 'stat', 'sys', 'time', 'threading'}, names)

    def test_no_channel_and_no_repair_calls(self):
        names, attrs = set(), set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            if isinstance(node, ast.Attribute):
                attrs.add(node.attr)
        for forbidden in ('print', 'open', 'input', 'eval', 'exec', '__import__'):
            self.assertNotIn(forbidden, names, forbidden)
        for forbidden in ('chmod', 'fchmod', 'chown', 'fchown', 'mkdir', 'makedirs', 'unlink', 'remove', 'rename', 'replace', 'symlink', 'link', 'system', 'popen', 'Popen', 'run',
                          'getenv', 'environ', 'argv', 'urandom', 'randbytes', 'getrandom', 'write', 'ftruncate', 'truncate'):
            self.assertNotIn(forbidden, attrs, forbidden)
        for banned in ('cryptography', 'keyring', 'subprocess', 'socket', 'logging', 'tempfile', 'shutil'):
            self.assertNotIn('import ' + banned, self.text)

    def test_the_reference_vocabulary_is_closed_and_documented(self):
        self.assertEqual(PK.REFERENCE_KEYS, frozenset({'kind', 'path', 'key_id', 'identity', 'project_id'}))
        self.assertEqual(PK.KIND, 'project-key-file')
        self.assertEqual(PK.KEY_BYTES, 32)
        self.assertEqual(PK.KEY_ID_PREFIX, 'pk1-')
        for reason in ('unlock_reference_invalid', 'unlock_reference_foreign', 'unlock_identity_mismatch', 'unlock_identity_privileged', 'unlock_path_unsafe', 'unlock_key_absent', 'unlock_key_unsafe',
                       'unlock_acl_present', 'unlock_mount_unsafe', 'unlock_key_mismatch', 'unlock_key_installation', 'unlock_io_failed', 'unlock_session_closed', 'unlock_session_expired',
                       'unlock_session_foreign_process', 'unlock_session_copy', 'unlock_deadline_invalid'):
            self.assertIn(reason, self.text)


if __name__ == '__main__':
    unittest.main()
