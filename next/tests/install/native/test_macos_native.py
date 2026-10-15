"""macOS native mechanics for the accepted design2 parts (W4 clock, Q2 arming, Q4 client).

Runs on macOS with the Xcode toolchain only; elsewhere every method is NOT_RUN
(skipped), never PASS. No launchd job, Mach service, sudo or signing is used, so
none of this is the M2 message-sender binding or the M3 signed closure: those
stay UNBOUND. The interpreter is whatever runs the suite, not the admitted
CPython 3.12.15 bundle.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest

from cortex_core.install import owner
from cortex_core.install.platforms import _clock

PLATFORMS = Path(__file__).resolve().parents[3] / 'src/cortex_core/install/platforms'
HERE = Path(__file__).resolve().parent
MAC = sys.platform == 'darwin' and shutil.which('clang') is not None and shutil.which('codesign') is not None
NOT_RUN = 'NOT_RUN: needs macOS + Xcode toolchain'
CFLAGS = ['-O2', '-Wall', '-Wextra', '-Werror', '-mmacosx-version-min=13.0']


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


def linked(path):
    out = run(['otool', '-L', str(path)]).stdout.splitlines()[1:]
    return sorted(line.split()[0] for line in out if line.strip())


@unittest.skipUnless(MAC, NOT_RUN)
class MacOSNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='cx-native-'))
        cls.ext = cls.tmp / ('_macos_native' + sysconfig.get_config_var('EXT_SUFFIX'))
        built = run(['clang', '-bundle', '-undefined', 'dynamic_lookup', *CFLAGS,
                     '-I' + sysconfig.get_paths()['include'], str(PLATFORMS / '_macos_native.c'), '-o', str(cls.ext)])
        cls.build = built
        if built.returncode == 0:
            spec = importlib.util.spec_from_file_location('_macos_native', cls.ext)
            cls.native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.native)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_mac_native_builds_warning_free_and_links_only_libsystem(self):
        self.assertEqual(self.build.returncode, 0, self.build.stderr)
        self.assertEqual(linked(self.ext), ['/usr/lib/libSystem.B.dylib'])

    def test_native_monotonic_sample_is_python_monotonic_domain(self):
        for _ in range(50):
            before = time.monotonic_ns()
            s = self.native.clock_sample()
            after = time.monotonic_ns()
            self.assertLessEqual(before, s['m0'])
            self.assertLessEqual(s['m0'], s['m1'])
            self.assertLessEqual(s['m1'], after)
            self.assertLess(abs(s['realtime_ns'] - time.time_ns()), 1_000_000_000)

    def test_native_sample_meets_kernel_clock_rule_or_refuses_only_by_rule(self):
        s = self.native.clock_sample()
        self.assertEqual(set(s), _clock._SAMPLE)
        self.assertEqual(s['platform'], 'darwin')
        bounded = (0 <= s['ntp_state'] < 5 and not s['ntp_status'] & 0x40 and 0 <= s['maxerror_us'] <= 500_000
                   and s['m1'] - s['m0'] <= 1_000_000)
        try:
            _clock.anchor(s)
            self.assertTrue(bounded, s)
        except owner.OwnerRefusal as refusal:  # e.g. timed has not refreshed maxerror yet
            self.assertEqual(refusal.detail, 'time_unavailable')
            self.assertFalse(bounded, s)

    def test_mac_native_requirement_set_before_resume_and_checked(self):
        driver = self.tmp / 'xpc_arm_driver'
        built = run(['clang', *CFLAGS, '-I' + str(PLATFORMS), str(HERE / 'xpc_arm_driver.c'), '-o', str(driver)])
        self.assertEqual(built.returncode, 0, built.stderr)
        cdhash = [l.split('=', 1)[1] for l in run(['codesign', '-dvvv', str(driver)]).stderr.splitlines()
                  if l.startswith('CDHash=')]
        self.assertEqual(len(cdhash), 1)
        self.assertEqual(run([str(driver), f'cdhash H"{cdhash[0]}"']).stdout.split(), ['ARMED', 'REPLY'])
        self.assertEqual(run([str(driver), 'identifier "ai.kaidera.not.this"']).stdout.split(), ['ARMED', 'NO_REPLY'])
        self.assertEqual(run([str(driver), 'not a requirement']).stdout.split(), ['ARM_REFUSED', 'NO_REPLY'])
        source = (PLATFORMS / '_macos_xpc.h').read_text()
        self.assertLess(source.index('set_peer_code_signing_requirement'), source.index('xpc_connection_resume'))

    def test_xpc_client_requires_build_inputs_and_links_only_libsystem(self):
        client = self.tmp / 'cortex-owner-client'
        src = str(PLATFORMS / '_owner_client.c')
        self.assertNotEqual(run(['clang', *CFLAGS, src, '-o', str(client)]).returncode, 0)  # no silent default
        built = run(['clang', *CFLAGS, '-DCX_SERVICE="ai.kaidera.cortex.owner"',
                     '-DCX_OWNER_REQUIREMENT="identifier \\"ai.kaidera.cortex.owner\\""', src, '-o', str(client)])
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(linked(client), ['/usr/lib/libSystem.B.dylib'])
        self.assertEqual(run([str(client)], input='').returncode, 2)  # empty request refuses before any IPC

    def test_posix_client_moves_bounded_bytes_without_ancillary(self):
        # POSIX mechanics of the Linux client branch, compiled on macOS (not Linux qualification).
        path = self.tmp / 'o.sock'
        client = self.tmp / 'posix-client'
        built = run(['clang', *CFLAGS, '-DCX_POSIX', f'-DCX_SOCKET="{path}"', str(PLATFORMS / '_owner_client.c'),
                     '-o', str(client)])
        self.assertEqual(built.returncode, 0, built.stderr)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path)); server.listen(1)
        seen = {}

        def serve():
            conn, _ = server.accept()
            data, flags = b'', 0
            while True:
                chunk, ancillary, f, _ = conn.recvmsg(65536, 1024)
                seen.setdefault('ancillary', []).extend(ancillary); flags |= f
                if not chunk:
                    break
                data += chunk
            seen['data'], seen['flags'] = data, flags
            conn.sendall(b'REPLY:' + data[:8]); conn.close()

        worker = threading.Thread(target=serve); worker.start()
        request = os.urandom(300_000)
        done = subprocess.run([str(client)], input=request, capture_output=True, timeout=60)
        worker.join(10); server.close()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(seen['data'], request)
        self.assertEqual(seen['ancillary'], [])
        self.assertEqual(done.stdout, b'REPLY:' + request[:8])
        path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path)); listener.listen(1); listener.setblocking(False)
        for refused in (b'x' * (1048576 + 1), b''):  # oversize / empty: refused before connecting
            self.assertEqual(subprocess.run([str(client)], input=refused, capture_output=True, timeout=60).returncode, 2)
        with self.assertRaises(BlockingIOError):
            listener.accept()
        listener.close()


if __name__ == '__main__':
    unittest.main()
