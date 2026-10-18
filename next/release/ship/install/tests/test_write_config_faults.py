"""S2 correction (HALE-S2-PUBLISH-001, HALE-S2-DIRIO-002): filesystem FAULT controls for config_writer.write_config against the ACTUAL Core private reader and decoder.
Derived from the SAME injections as Hale's raw fs_controls.py (copied unchanged into the packet, sha256 b82cc7f6…02e2a) but asserting the REQUIRED all-or-none outcome, not the observed defect:
a failed publication leaves no accepted final and no two-link unreadable final; a reported success leaves the one private one-link file Core accepts, with the directory made durable AFTER the
temp name is removed; a persistent secondary cleanup failure is a distinct typed non-success, never success; a failed new-directory fchmod/check is a typed refusal and its descriptor is closed.
Only selected os.* filesystem calls are fault-injected (fake boundary, named per test); owned NONSECRET temporary fixtures; no process, network, key, signing or custody. This module does not
import test_sign, Release or _fixtures. Deciding predicates are committed RED first and are unchanged by the fix commits."""
import contextlib
import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
NEXT = HERE.parents[3]
sys.path[:0] = [str(INSTALL), str(NEXT / 'src')]
import config_writer as W  # noqa: E402

PROJECT, PORT = 'helix', 8123
TEMP_PREFIX = '.cortex.json.'


def is_dir(fd):
    return stat.S_ISDIR(os.fstat(fd).st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-s2fix-')))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.vault = self.tmp / 'vault'
        self.vault.mkdir(mode=0o700)
        self.final = self.vault / PROJECT / 'config' / 'cortex.json'

    def call(self, port=PORT):
        try:
            return ('ok', W.write_config(str(self.vault), PROJECT, port)['result'])
        except W.Refusal as refusal:
            return ('refused', refusal.reason)
        except Exception as error:   # a behavioural outcome (a raw error is the DIRIO defect), never a setup error
            return ('raw ' + type(error).__name__, getattr(error, 'errno', None))

    def names(self):
        return sorted(p.name for p in self.final.parent.iterdir()) if self.final.parent.exists() else []

    def core_accepts(self):
        try:
            W._read_back(str(self.final))
            return True
        except W.Refusal:
            return False

    def nlink(self):
        return os.lstat(self.final).st_nlink

    def assert_clean_retry(self):
        """After the fault is removed a retry must publish the one private one-link file that Core accepts."""
        self.assertEqual(self.call(), ('ok', 'written'))
        st = os.lstat(self.final)
        self.assertEqual((st.st_nlink, stat.S_IMODE(st.st_mode)), (1, 0o600))
        self.assertTrue(self.core_accepts())


class Publication(Base):
    """HALE-S2-PUBLISH-001. FAKE BOUNDARY: os.unlink / os.fsync return EIO for chosen targets."""

    def test_a_temp_unlink_failure_never_strands_a_two_link_final_and_never_reports_success(self):
        real = os.unlink

        def unlink(path, *a, **kw):
            if str(path).startswith(TEMP_PREFIX):
                raise OSError(errno.EIO, 'injected temp unlink EIO')
            return real(path, *a, **kw)
        with mock.patch.object(W.os, 'unlink', unlink):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertFalse(os.path.lexists(self.final), 'no final (a two-link file Core refuses) may be left behind')
        leftovers = self.names()
        self.assertEqual(len(leftovers), 1, leftovers)    # the one temp leaf whose removal failed: the disclosed remaining state
        self.assertTrue(leftovers[0].startswith(TEMP_PREFIX))
        self.assertEqual(os.lstat(self.final.parent / leftovers[0]).st_nlink, 1)
        self.assert_clean_retry()

    def test_a_persistent_cleanup_failure_is_a_distinct_typed_non_success_and_core_still_refuses_the_state(self):
        real = os.unlink

        def unlink(path, *a, **kw):
            if str(path).startswith(TEMP_PREFIX) or str(path) == 'cortex.json':
                raise OSError(errno.EIO, 'injected persistent unlink EIO')
            return real(path, *a, **kw)
        with mock.patch.object(W.os, 'unlink', unlink):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_publish_incomplete'))
        self.assertEqual(self.nlink(), 2, 'the remaining state is stated exactly: final plus temp, two links')
        self.assertFalse(self.core_accepts(), 'Core must keep refusing the two-link state; it is never reported as success')

    def test_a_directory_fsync_failure_after_the_link_does_not_leave_an_accepted_final(self):
        real = os.fsync

        def fsync(fd):
            if is_dir(fd):
                raise OSError(errno.EIO, 'injected directory fsync EIO')
            return real(fd)
        with mock.patch.object(W.os, 'fsync', fsync):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertFalse(os.path.lexists(self.final), 'a reported failure must not leave a published identity Core accepts')
        self.assertEqual(self.names(), [], 'and no temp file')
        self.assert_clean_retry()

    def test_a_failure_of_the_directory_fsync_after_the_temp_removal_is_not_a_success(self):
        real = os.fsync
        seen = []

        def fsync(fd):
            if is_dir(fd):
                seen.append('dir')
                if len(seen) == 2:
                    raise OSError(errno.EIO, 'injected post-removal directory fsync EIO')
            return real(fd)
        with mock.patch.object(W.os, 'fsync', fsync):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertFalse(os.path.lexists(self.final))
        self.assertEqual(self.names(), [])

    def test_the_no_fault_publication_order_ends_with_a_directory_fsync_after_the_temp_is_removed(self):
        events = []
        real_sync, real_link, real_unlink = os.fsync, os.link, os.unlink

        def sync(fd):
            events.append('fsync_directory' if is_dir(fd) else 'fsync_file')
            return real_sync(fd)

        def link(*a, **kw):
            events.append('link_final')
            return real_link(*a, **kw)

        def unlink(*a, **kw):
            events.append('unlink_temp')
            return real_unlink(*a, **kw)
        with mock.patch.object(W.os, 'fsync', sync), mock.patch.object(W.os, 'link', link), mock.patch.object(W.os, 'unlink', unlink):
            outcome = self.call()
        self.assertEqual(outcome, ('ok', 'written'))
        self.assertEqual(events, ['fsync_file', 'link_final', 'fsync_directory', 'unlink_temp', 'fsync_directory'])
        st = os.lstat(self.final)
        self.assertEqual((st.st_nlink, stat.S_IMODE(st.st_mode), self.names()), (1, 0o600, ['cortex.json']))
        self.assertTrue(self.core_accepts())

    # ---- no-weakening controls: expected to PASS before and after the fix --------------------------------------------------------------------------------------
    def test_an_existing_different_config_is_never_overwritten_and_no_temp_is_left(self):
        self.assertEqual(self.call(), ('ok', 'written'))
        before = self.final.read_bytes()
        self.assertEqual(self.call(port=PORT + 1), ('refused', 'config_exists_different'))
        self.assertEqual((self.final.read_bytes(), self.names()), (before, ['cortex.json']))

    def test_losing_a_concurrent_link_race_keeps_the_winner_untouched_and_removes_only_our_temp(self):
        self.assertEqual(self.call(), ('ok', 'written'))
        winner = self.final.read_bytes()
        real = os.stat

        def hide(path, *a, **kw):   # make the pre-check see no leaf, so the writer reaches link and loses the race
            if str(path) == 'cortex.json' and kw.get('dir_fd') is not None and kw.get('follow_symlinks') is False and not hide.once:
                hide.once = True
                raise FileNotFoundError(path)
            return real(path, *a, **kw)
        hide.once = False
        with mock.patch.object(W.os, 'stat', hide):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_exists_different'))
        self.assertEqual((self.final.read_bytes(), self.names()), (winner, ['cortex.json']))

    def test_a_rollback_never_removes_a_final_that_is_not_the_one_this_writer_linked(self):
        real = os.fsync
        foreign = b'{"foreign": "another installation"}'

        def fsync(fd):
            if is_dir(fd):
                os.unlink(self.final)   # another actor replaces the final between our link and our rollback
                self.final.write_bytes(foreign)
                self.final.chmod(0o600)
                raise OSError(errno.EIO, 'injected directory fsync EIO')
            return real(fd)
        with mock.patch.object(W.os, 'fsync', fsync):
            outcome = self.call()
        self.assertEqual(outcome[0], 'refused')
        self.assertEqual(self.final.read_bytes(), foreign, 'identity check: only our own link may be rolled back')


class DirectoryDescriptor(Base):
    """HALE-S2-DIRIO-002. FAKE BOUNDARY: os.fchmod / os.fstat return EIO for the newly created project directory."""

    def inject_fchmod(self):
        real = os.fchmod
        opened = []

        def fchmod(fd, mode):
            if is_dir(fd):
                opened.append(fd)
                raise OSError(errno.EIO, 'injected directory fchmod EIO')
            return real(fd, mode)
        return opened, mock.patch.object(W.os, 'fchmod', fchmod)

    def leaked(self, fds):
        out = []
        for fd in fds:
            try:
                os.fstat(fd)
                out.append(fd)
            except OSError as error:
                self.assertEqual(error.errno, errno.EBADF)
        for fd in out:
            os.close(fd)   # keep the test process clean even when the defect is present
        return out

    def test_a_directory_fchmod_failure_is_a_typed_refusal_and_the_new_descriptor_is_closed(self):
        opened, patch = self.inject_fchmod()
        with patch:
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.leaked(opened), [], 'the newly acquired descriptor must be closed (fstat EBADF)')
        self.assertFalse(os.path.lexists(self.final))

    def test_a_directory_check_failure_is_a_typed_refusal_and_the_new_descriptor_is_closed(self):
        real = os.fstat
        opened = []

        def fstat(fd):
            st = real(fd)
            if stat.S_ISDIR(st.st_mode) and (self.vault / PROJECT).exists() and st.st_ino == os.stat(self.vault / PROJECT).st_ino and sys._getframe(1).f_code.co_name == '_check_dir':
                opened.append(fd)
                raise OSError(errno.EIO, 'injected directory check EIO')
            return st
        with mock.patch.object(W.os, 'fstat', fstat):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.leaked(opened), [])

    def test_a_directory_check_failure_on_an_existing_directory_is_typed_and_its_descriptor_is_closed(self):   # added after the mutant run: the existing-directory path of the same check
        (self.vault / PROJECT).mkdir(mode=0o700)
        (self.vault / PROJECT / 'config').mkdir(mode=0o700)
        target = os.stat(self.vault / PROJECT / 'config').st_ino
        real = os.fstat
        opened = []

        def fstat(fd):
            st = real(fd)
            if stat.S_ISDIR(st.st_mode) and st.st_ino == target and sys._getframe(1).f_code.co_name == '_check_dir':
                opened.append(fd)
                raise OSError(errno.EIO, 'injected directory check EIO')
            return st
        with mock.patch.object(W.os, 'fstat', fstat):
            outcome = self.call()
        self.assertEqual(outcome, ('refused', 'config_write_failed'))
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.leaked(opened), [])

    def test_the_cli_keeps_its_typed_exit_two_contract_under_the_same_fault(self):
        opened, patch = self.inject_fchmod()
        err = io.StringIO()
        with patch, contextlib.redirect_stderr(err):
            try:
                code = W.main(['--vault', str(self.vault), '--project', PROJECT, '--port', str(PORT)])
            except Exception as error:
                code = 'raw ' + type(error).__name__
        self.leaked(opened)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue().strip().splitlines()[-1]), {'reason': 'config_write_failed', 'unit': 'write_config'})

    def test_a_successful_write_leaves_no_extra_open_descriptor(self):   # control: expected to pass before and after
        before = sorted(os.listdir('/dev/fd'))
        self.assertEqual(self.call(), ('ok', 'written'))
        self.assertEqual(sorted(os.listdir('/dev/fd')), before)


if __name__ == '__main__':
    unittest.main()
