"""The fixed launch descriptors and the SELinux policy source are reviewed DATA
(design2 Q2/Q6/Q9 + sec. 3, design3 sec. 2.3).

Source checks of the text only: no launchctl/systemctl, no install. Final paths
belong to W7 packaging; these checks pin the security-relevant shape.
"""
from pathlib import Path
import plistlib
import re
import unittest

LAUNCH = Path(__file__).resolve().parents[2] / 'release/install/launch'
POLICY = LAUNCH / 'linux/selinux/kaidera_cortex'


def policy():
    """(allow rules as (source, target, class, perms), macro calls as (name, args), raw text) of the .te."""
    text = re.sub(r'#[^\n]*', '', (POLICY.with_suffix('.te')).read_text())
    rules = []
    for src, tgt, cls, perms in re.findall(r'\ballow\s+(\S+)\s+([^\s:]+):(\S+)\s+(\{[^}]*\}|\S+?)\s*;', text):
        rules.append((src, tgt, cls, set(perms.strip('{} ').split())))
    calls = [(name, [a.strip() for a in args.split(',')]) for name, args in re.findall(r'^(\w+)\(([^)]*)\)', text, re.M)]
    return rules, calls, text


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
                                 'linux/selinux/kaidera_cortex.fc', 'linux/selinux/kaidera_cortex.te',
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


    def test_selinux_only_c_reaches_v_and_c_is_entered_only_by_its_file(self):
        rules, calls, text = policy()
        self.assertEqual([(s, p) for s, t, c, p in rules if 'connectto' in p], [('cortex_c_t', {'connectto'})])
        self.assertEqual([r[1] for r in rules if 'connectto' in r[3]], ['cortex_v_t'])
        self.assertEqual([s for s, t, c, p in rules if t == 'cortex_v_sock_t' and 'write' in p], ['cortex_c_t'])
        self.assertEqual([a for n, a in calls if n == 'domain_entry_file'], [['cortex_c_t', 'cortex_c_exec_t']])
        self.assertEqual([a for n, a in calls if n == 'domain_auto_trans'], [['user_t', 'cortex_c_exec_t', 'cortex_c_t']])
        self.assertFalse(any('entrypoint' in p or 'transition' in p for s, t, c, p in rules))

    def test_selinux_c_is_a_leaf_nobody_touches_c_and_no_capabilities(self):
        rules, calls, text = policy()
        forbidden = {'execute', 'execute_no_trans', 'execmem', 'execstack', 'execheap', 'ptrace', 'transition',
                     'dyntransition', 'setexec', 'setcurrent', 'relabelfrom', 'relabelto'}
        self.assertFalse([r for r in rules if r[0] == 'cortex_c_t' and r[3] & forbidden])
        # others reach C only to stop it: the calling user may signal its own client (lab 16 Oct), nothing else
        self.assertEqual([r for r in rules if r[1] == 'cortex_c_t' and r[0] != 'cortex_c_t'],
                         [('user_t', 'cortex_c_t', 'process', {'sigchld', 'sigkill', 'sigstop', 'signull', 'signal'})])
        self.assertFalse([r for r in rules if r[3] & {'ptrace', 'relabelfrom', 'relabelto'}])
        self.assertFalse([r for r in rules if r[2] in ('capability', 'capability2', 'cap_userns', 'cap2_userns')])
        self.assertEqual(sorted(n for n, a in calls if 'cortex_c_t' in a and n not in ('domain_type', 'domain_entry_file',
                         'domain_auto_trans')), ['files_read_etc_files', 'files_search_pids', 'libs_use_ld_so',
                         'libs_use_shared_libs', 'userdom_use_user_terminals'])  # no exec/corecmd/can_exec macro
        self.assertNotRegex(text, r'\bpermissive\b')
        self.assertNotRegex(text, r'\b(can_exec|corecmd_exec_\w*|neverallow|unconfined_\w*)\(')

    def test_selinux_v_observes_effective_policy_without_capability(self):
        rules, calls, text = policy()
        v = {n for n, a in calls if a == ['cortex_v_t']}
        self.assertLessEqual({'selinux_read_policy', 'selinux_get_enforce_mode', 'selinux_get_fs_mount'}, v)
        self.assertEqual([a for n, a in calls if n == 'init_daemon_domain'], [['cortex_v_t', 'cortex_v_exec_t']])
        self.assertIn('class security read_policy;', text)
        # NoNewPrivileges=yes on V's unit: without nnp_transition the exec stays in init_t (lab 16 Oct)
        svc = unit(LAUNCH / 'linux/kaidera-cortex-owner.service')['Service']
        self.assertEqual(svc['NoNewPrivileges'], ['yes'])
        self.assertIn(('init_t', 'cortex_v_t', 'process2', {'nnp_transition', 'nosuid_transition'}), rules)
        self.assertFalse([r for r in rules if r[0] == 'cortex_v_t' and r[3] & {'execute_no_trans', 'transition', 'ptrace'}])

    def test_selinux_file_contexts_match_the_descriptors(self):
        fc = {}
        for line in POLICY.with_suffix('.fc').read_text().splitlines():
            path, *_, ctx = line.split()
            fc[path.replace('\\.', '.')] = ctx.split(':')[2].split(',')[0]
        socket = unit(LAUNCH / 'linux/kaidera-cortex-owner.socket')['Socket']['ListenStream'][0]
        svc = unit(LAUNCH / 'linux/kaidera-cortex-owner.service')['Service']
        exe, script = svc['ExecStart'][0].split()[0], svc['ExecStart'][0].split()[4]
        self.assertEqual(fc[socket], 'cortex_v_sock_t')
        self.assertEqual(fc[str(Path(socket).parent)], 'cortex_owner_run_t')
        self.assertEqual(fc[exe], 'cortex_v_exec_t')
        runtime = [p for p, t in fc.items() if t == 'cortex_runtime_t']
        self.assertEqual(len(runtime), 1)
        self.assertTrue(script.startswith(runtime[0].removesuffix('(/.*)?') + '/'))
        self.assertEqual(fc[svc['ReadWritePaths'][0] + '(/.*)?'], 'cortex_v_state_t')
        self.assertEqual([t for t in fc.values() if t == 'cortex_c_exec_t'], ['cortex_c_exec_t'])
        _, _, text = policy()
        declared = set(re.findall(r'^type (\w+);', text, re.M))
        self.assertLessEqual(set(fc.values()), declared)

if __name__ == '__main__':
    unittest.main()
