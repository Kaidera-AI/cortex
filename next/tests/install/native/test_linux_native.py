"""Linux native mechanics for the accepted parts (W4 clock, Q6 fd-3 listener, design3 effective policy,
SELinux policy source compile).

Runs on Linux with a C compiler only; on macOS every method is NOT_RUN (skipped),
never PASS. Same-uid mechanics: no SELinux policy, account split, systemd unit or
host qualification is exercised here (W2 sender binding stays HELD/UNBOUND).
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

from cortex_core.install import owner
from cortex_core.install.platforms import _clock, _selinux

PLATFORMS = Path(__file__).resolve().parents[3] / 'src/cortex_core/install/platforms'
HERE = Path(__file__).resolve().parent
SELINUX_SRC = Path(__file__).resolve().parents[3] / 'release/install/launch/linux/selinux'
DEVEL = Path('/usr/share/selinux/devel/Makefile')
CC = shutil.which('cc') or shutil.which('gcc')
LINUX = sys.platform.startswith('linux') and CC is not None
NOT_RUN = 'NOT_RUN: needs Linux + C compiler'
CFLAGS = ['-O2', '-Wall', '-Wextra', '-Werror']


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


@unittest.skipUnless(LINUX, NOT_RUN)
class LinuxNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='cx-native-'))
        cls.ext = cls.tmp / ('_linux_native' + sysconfig.get_config_var('EXT_SUFFIX'))
        cls.build = run([CC, '-shared', '-fPIC', *CFLAGS, '-I' + sysconfig.get_paths()['include'],
                         str(PLATFORMS / '_linux_native.c'), '-o', str(cls.ext)])
        if cls.build.returncode == 0:
            spec = importlib.util.spec_from_file_location('_linux_native', cls.ext)
            cls.native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.native)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_linux_native_builds_warning_free(self):
        self.assertEqual(self.build.returncode, 0, self.build.stderr)

    def test_native_monotonic_sample_is_python_monotonic_domain(self):
        for _ in range(50):
            before = time.monotonic_ns()
            s = self.native.clock_sample()
            after = time.monotonic_ns()
            self.assertLessEqual(before, s['m0'])
            self.assertLessEqual(s['m0'], s['m1'])
            self.assertLessEqual(s['m1'], after)

    def test_native_sample_meets_kernel_clock_rule_or_refuses_only_by_rule(self):
        s = self.native.clock_sample()
        self.assertEqual(set(s), _clock._SAMPLE)
        bounded = (0 <= s['ntp_state'] < 5 and not s['ntp_status'] & 0x40 and 0 <= s['maxerror_us'] <= 500_000
                   and s['m1'] - s['m0'] <= 1_000_000)
        try:
            _clock.anchor(s)
            self.assertTrue(bounded, s)
        except owner.OwnerRefusal as refusal:  # e.g. an unsynchronised CI runner
            self.assertEqual(refusal.detail, 'time_unavailable')
            self.assertFalse(bounded, s)

    def test_inherited_listener_is_verified_not_trusted(self):
        driver = self.tmp / 'listener_driver'
        built = run([CC, *CFLAGS, '-I' + str(PLATFORMS), str(HERE / 'listener_driver.c'), '-o', str(driver)])
        self.assertEqual(built.returncode, 0, built.stderr)
        path = str(self.tmp / 'owner.sock')
        expected = {'ok': 'VERIFIED', 'unlistened': 'REFUSED', 'dgram': 'REFUSED', 'wrongpath': 'REFUSED',
                    'replaced': 'REFUSED'}
        for case, verdict in expected.items():
            with self.subTest(case=case):
                self.assertEqual(run([str(driver), path, case]).stdout.strip(), verdict)

    def test_effective_policy_observation_reads_the_loaded_kernel_policy(self):
        if not Path('/sys/fs/selinux/status').exists():
            self.skipTest('NOT_RUN: SELinux not mounted on this host')
        member = _selinux.verdict(_selinux.start())
        self.assertEqual(member['s0'], member['s1'])
        self.assertEqual(len(member['policy_sha256']), 64)
        self.assertEqual(_selinux.start()['policy_sha256'], member['policy_sha256'])  # stable while unchanged (L-P1)


    def test_selinux_policy_source_compiles_against_the_host_policy(self):
        # Compile only (no semodule -i): load + behaviour are lab receipts, never CI.
        if not DEVEL.exists() or shutil.which('make') is None:
            self.skipTest('NOT_RUN: needs selinux-policy-devel + make')
        for name in ('kaidera_cortex.te', 'kaidera_cortex.fc'):
            shutil.copy(SELINUX_SRC / name, self.tmp / name)
        built = run(['make', '-f', str(DEVEL), 'kaidera_cortex.pp'], cwd=self.tmp)
        self.assertEqual(built.returncode, 0, built.stdout[-2000:] + built.stderr[-2000:])
        self.assertTrue((self.tmp / 'kaidera_cortex.pp').stat().st_size > 0)

if __name__ == '__main__':
    unittest.main()
