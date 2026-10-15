"""The fixed launch descriptors are reviewed DATA (design2 Q2/Q6/Q9).

Source checks of the text only: no launchctl/systemctl, no install. Final paths
belong to W7 packaging; these checks pin the security-relevant shape.
"""
from pathlib import Path
import plistlib
import unittest

LAUNCH = Path(__file__).resolve().parents[2] / 'release/install/launch'


def unit(path):
    sections, current = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            current = sections.setdefault(line.strip('[]'), {})
            continue
        key, _, value = line.partition('=')
        current.setdefault(key, []).append(value)
    return sections


class LaunchDescriptors(unittest.TestCase):
    def test_only_the_three_reviewed_descriptors_exist(self):
        found = sorted(str(p.relative_to(LAUNCH)) for p in LAUNCH.rglob('*') if p.is_file())
        self.assertEqual(found, ['linux/kaidera-cortex-owner.service', 'linux/kaidera-cortex-owner.socket',
                                 'macos/ai.kaidera.cortex.owner.plist'])

    def test_macos_daemon_is_on_demand_nonroot_fixed_isolated_python(self):
        plist = plistlib.loads((LAUNCH / 'macos/ai.kaidera.cortex.owner.plist').read_bytes())
        self.assertEqual(set(plist), {'Label', 'MachServices', 'ProgramArguments', 'UserName', 'GroupName',
                                      'RunAtLoad', 'ProcessType'})
        self.assertEqual(plist['MachServices'], {plist['Label']: True})
        self.assertNotIn(plist['UserName'], ('root', '_root', ''))
        self.assertEqual(plist['UserName'], plist['GroupName'])
        self.assertIs(plist['RunAtLoad'], False)  # on demand only; no KeepAlive (finite V)
        args = plist['ProgramArguments']
        self.assertEqual(args[1:4], ['-I', '-S', '-B'])
        self.assertEqual(len(args), 5)
        self.assertTrue(all(a.startswith('/Library/Application Support/Kaidera/Cortex/') for a in (args[0], args[4])))

    def test_linux_socket_is_pathname_stream_accept_no_caller_group_only(self):
        s = unit(LAUNCH / 'linux/kaidera-cortex-owner.socket')['Socket']
        self.assertEqual(s['ListenStream'], ['/run/kaidera-cortex/owner.sock'])
        self.assertEqual(s['Accept'], ['no'])
        self.assertEqual(s['SocketMode'], ['0660'])
        self.assertNotEqual(s['SocketGroup'], s['SocketUser'])  # callers are never in V's group
        self.assertEqual(s['RemoveOnStop'], ['no'])
        svc = unit(LAUNCH / 'linux/kaidera-cortex-owner.service')['Service']
        self.assertEqual(s['SocketUser'], svc['User'])
        self.assertNotEqual(s['SocketGroup'], svc['Group'])

    def test_linux_service_nonroot_zero_capabilities_hardened_without_protectproc(self):
        svc = unit(LAUNCH / 'linux/kaidera-cortex-owner.service')['Service']
        self.assertNotIn(svc['User'][0], ('root', '0'))
        for key, value in (('NoNewPrivileges', 'yes'), ('CapabilityBoundingSet', ''), ('AmbientCapabilities', ''),
                           ('RestrictAddressFamilies', 'AF_UNIX'), ('ProtectSystem', 'strict'),
                           ('PrivateTmp', 'yes'), ('LockPersonality', 'yes'), ('MemoryDenyWriteExecute', 'yes'),
                           ('SystemCallFilter', '@system-service')):
            self.assertEqual(svc[key], [value], key)
        self.assertNotIn('ProtectProc', svc)  # V must read /proc/<C>
        self.assertNotIn('Environment', svc)
        self.assertNotIn('EnvironmentFile', svc)
        exec_start = svc['ExecStart'][0].split()
        self.assertEqual(exec_start[1:4], ['-I', '-S', '-B'])
        self.assertTrue(all(p.startswith('/opt/kaidera/cortex/') for p in (exec_start[0], exec_start[4])))


if __name__ == '__main__':
    unittest.main()
