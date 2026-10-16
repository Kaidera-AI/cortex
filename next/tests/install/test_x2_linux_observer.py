"""X2 Linux owner-binding fixture contract; no host or native qualification."""
import copy
import errno
import stat
import sys
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.install.platforms import linux
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
from test_gate_b_linux import LinuxObserverOracle, SUBJECTS


class X2LinuxObserverBinding(unittest.TestCase):
    def test_fixed_owner_supplier_delegates_to_linux_zero_argument_provider(self):
        expected = LinuxObserverOracle().inputs("positive")
        with patch.object(sys, "platform", "linux"), \
             patch.object(linux, "_installed_linux_inputs", return_value=expected) as provider:
            try:
                result = owner._installed_owner_inputs()
            except owner.OwnerRefusal as error:
                self.fail("X2 Linux provider was not reached: " + error.detail)
        self.assertIs(type(result), owner._Inputs)
        self.assertIs(result, expected)
        provider.assert_called_once_with()


class _FakeLinuxSyscalls:
    """Synthetic syscall outputs; no process, namespace, or backend is touched."""

    def __init__(self, receipt):
        self.receipt = copy.deepcopy(receipt)
        self.events = []
        self.process_overrides = {}

    def table(self):
        r = self.receipt
        credentials = r['transport']['credentials']
        transport = {key: r['transport'][key] for key in
                     ('family', 'type', 'kind', 'source', 'endpoint')}
        roles = {
            r['peer_process']['pid']: 'peer',
            r['owner_process']['pid']: 'owner',
        }

        def peer_transport(fd):
            self.events.append(('peer_transport', fd))
            return copy.deepcopy(transport)

        def peer_credentials(fd):
            self.events.append(('peer_credentials', fd))
            return (credentials['pid'], credentials['uid'], credentials['gid'])

        def peer_pidfd(fd):
            self.events.append(('peer_pidfd', fd))
            return 701

        def pidfd_open(pid):
            self.events.append(('pidfd_open', pid))
            return 702

        def pidfd_alive(fd):
            self.events.append(('pidfd_alive', fd))
            return True

        def pidfd_pid(fd):
            self.events.append(('pidfd_pid', fd))
            return r['peer_process']['pid'] if fd == 701 else r['owner_process']['pid']

        def process_snapshot(pid, pidfd):
            self.events.append(('process_snapshot', pid, pidfd))
            role = roles[pid]
            value = copy.deepcopy(r[role + '_process'])
            override = self.process_overrides.get((role, sum(
                1 for event in self.events if event[0] == 'process_snapshot' and event[1] == pid)))
            if override:
                override(value)
            return {'process': value,
                    'capabilities': copy.deepcopy(r['privileges']['capabilities']),
                    'no_new_privs': r['privileges']['no_new_privs']}

        def namespace_snapshot(pid, pidfd):
            self.events.append(('namespace_snapshot', pid, pidfd))
            return copy.deepcopy(r['namespaces'][roles[pid]])

        def maps_snapshot(pid, pidfd):
            self.events.append(('maps_snapshot', pid, pidfd))
            return copy.deepcopy(r['maps'][roles[pid]])

        objects = {obj['path']: obj for obj in r['objects']}

        def object_snapshot(path):
            self.events.append(('object_snapshot', path))
            return copy.deepcopy(objects[path])

        def startup_snapshot(pid, pidfd):
            self.events.append(('startup_snapshot', pid, pidfd))
            return copy.deepcopy(r['startup'])

        def backend_denials(pid, pidfd):
            self.events.append(('backend_denials', pid, pidfd))
            return copy.deepcopy(r['privileges']['paths'])

        def pidfd_close(fd):
            self.events.append(('pidfd_close', fd))

        return {
            'host_snapshot': lambda: dict(os='linux', distribution='rocky', version='10.2',
                architecture='x86_64', kernel='6.12.0-211.16.1.el10_2', selinux='enforcing'),
            'peer_transport': peer_transport,
            'peer_credentials': peer_credentials,
            'peer_pidfd': peer_pidfd,
            'pidfd_open': pidfd_open,
            'pidfd_pid': pidfd_pid,
            'pidfd_alive': pidfd_alive,
            'pidfd_close': pidfd_close,
            'self_pid': lambda: r['owner_process']['pid'],
            'process_snapshot': process_snapshot,
            'namespace_snapshot': namespace_snapshot,
            'maps_snapshot': maps_snapshot,
            'object_snapshot': object_snapshot,
            'startup_snapshot': startup_snapshot,
            'backend_denials': backend_denials,
        }


class X2LinuxCollector(unittest.TestCase):
    def setUp(self):
        self.base = LinuxObserverOracle().inputs('positive')
        context = owner._decode(self.base.context_raw, 'owner_unavailable')
        peer = owner._original(self.base, context['peer_ref'])
        policy_original = owner._original(self.base, context['policy_ref'])
        self.policy = policy_original['linux']
        self.receipt = owner._original(self.base, peer['linux_ref'])

    def collect(self, fake):
        bindings = {'inputs': self.base, 'peer_fd': 700}
        with patch.object(linux, '_LINUX_BINDING', bindings, create=True), \
             patch.object(linux, '_LINUX_SYSCALLS', fake.table(), create=True):
            return linux._installed_owner_inputs()

    def propose(self, fake, mutate=None):
        table = fake.table()
        if mutate is not None:
            mutate(table)
        events = []
        peer_transport = table['peer_transport']

        def observing_transport(fd):
            events.append('observe')
            return peer_transport(fd)

        table['peer_transport'] = observing_transport
        signed = NonAuthoritativeSignedMemberDouble(
            SUBJECTS['runtime_lock_raw'].encode(), aggregate_id=self.base.aggregate_id,
            member_name='runtime.lock.json')
        read_original = signed.read_original

        def audited_read(limit):
            events.append('r01-read')
            return read_original(limit)

        with patch.object(sys, 'platform', 'linux'), \
             patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
             patch.object(linux, '_LINUX_SYSCALLS', table, create=True), \
             patch.object(signed, 'read_original', audited_read), \
             signed.installed(signed_member):
            proposal = owner.propose_effect(SUBJECTS['request_raw'].encode())
        return proposal, events

    def test_fake_collector_returns_valid_data_and_holds_both_pidfds_until_done(self):
        fake = _FakeLinuxSyscalls(self.receipt)
        result = self.collect(fake)
        self.assertIs(type(result), owner._Inputs)
        self.assertTrue(any(event[0] == 'backend_denials' for event in fake.events))
        self.assertEqual([e for e in fake.events if e[0] == 'pidfd_close'],
                         [('pidfd_close', 702), ('pidfd_close', 701)])
        live_observations = {'process_snapshot', 'namespace_snapshot', 'maps_snapshot',
                             'object_snapshot', 'startup_snapshot', 'backend_denials'}
        self.assertLess(max(i for i, e in enumerate(fake.events) if e[0] in live_observations),
                        min(i for i, e in enumerate(fake.events) if e[0] == 'pidfd_close'))

    def test_peer_pid_uid_and_gid_mismatch_refuse_before_process_observation(self):
        for field in ('pid', 'uid', 'gid'):
            with self.subTest(field=field):
                fake = _FakeLinuxSyscalls(self.receipt)
                original = fake.table()['peer_credentials']

                def wrong(fd, original=original, field=field):
                    values = list(original(fd))
                    values[{'pid': 0, 'uid': 1, 'gid': 2}[field]] += 1
                    return tuple(values)

                table = fake.table()
                table['peer_credentials'] = wrong
                with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
                     patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        linux._installed_owner_inputs()
                self.assertEqual(got.exception.detail, 'peer_denied')
                self.assertFalse(any(e[0] == 'process_snapshot' for e in fake.events))

    def test_connected_pathname_transport_and_pidfd_targets_are_bound(self):
        for family in ('transport', 'peer_pidfd', 'owner_pidfd'):
            with self.subTest(family=family):
                fake = _FakeLinuxSyscalls(self.receipt)
                table = fake.table()
                if family == 'transport':
                    original = table['peer_transport']

                    def wrong_transport(fd, original=original):
                        value = original(fd)
                        value['endpoint'] += '.other'
                        return value

                    table['peer_transport'] = wrong_transport
                else:
                    original = table['pidfd_pid']
                    target_fd = 701 if family == 'peer_pidfd' else 702

                    def wrong_target(fd, original=original, target_fd=target_fd):
                        value = original(fd)
                        return value + 1 if fd == target_fd else value

                    table['pidfd_pid'] = wrong_target
                with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
                     patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        linux._installed_owner_inputs()
                self.assertEqual(got.exception.detail, 'peer_denied')

    def test_held_pidfd_detects_peer_pid_reuse_or_lifetime_drift(self):
        fake = _FakeLinuxSyscalls(self.receipt)
        fake.process_overrides[('peer', 2)] = lambda value: value.__setitem__('start_ticks', value['start_ticks'] + 1)
        with self.assertRaises(owner.OwnerRefusal) as got:
            self.collect(fake)
        self.assertEqual(got.exception.detail, 'peer_denied')
        self.assertEqual([e[1] for e in fake.events if e[0] == 'pidfd_close'], [702, 701])

    def test_code_and_ancestor_custody_mismatch_refuses(self):
        cases = (('interpreter', 'identity'), ('interpreter', 'sha256'),
                 ('source', 'sha256'), ('ancestor', 'identity'), ('endpoint', 'identity'))
        for role, field in cases:
            with self.subTest(role=role, field=field):
                fake = _FakeLinuxSyscalls(self.receipt)
                path = next(obj['path'] for obj in self.receipt['objects'] if obj['kind'] == {
                    'interpreter': 'file', 'source': 'file', 'ancestor': 'directory', 'endpoint': 'socket'
                }[role] and (obj.get('path') == self.policy['endpoint'] if role == 'endpoint' else
                             obj['path'] == next((item['path'] for item in self.policy['objects']
                                                  if item['role'] == role), None)))
                table = fake.table()
                snapshot = table['object_snapshot']

                def changed(path_value, snapshot=snapshot, path=path):
                    value = snapshot(path_value)
                    if path_value == path:
                        if field == 'identity':
                            value['held_identity'][1] += 1
                        else:
                            value['sha256'] = ('0' if value['sha256'][0] != '0' else '1') + value['sha256'][1:]
                    return value

                table['object_snapshot'] = changed
                with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
                     patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        linux._installed_owner_inputs()
                self.assertEqual(got.exception.detail, 'custody_changed')

    def test_namespace_or_map_drift_refuses(self):
        for family in ('namespace', 'uid_map', 'gid_map'):
            with self.subTest(family=family):
                fake = _FakeLinuxSyscalls(self.receipt)
                table = fake.table()
                key = 'namespace_snapshot' if family == 'namespace' else 'maps_snapshot'
                original = table[key]

                def drift(pid, pidfd, original=original, family=family):
                    value = original(pid, pidfd)
                    if family == 'namespace':
                        value['user'][1] += 1
                    else:
                        value['uid' if family == 'uid_map' else 'gid'][0][1] += 1
                    return value

                table[key] = drift
                with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
                     patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        linux._installed_owner_inputs()
                self.assertEqual(got.exception.detail, 'peer_denied')

    def test_backend_bypass_must_be_reported_denied(self):
        for probe in sorted(linux._PROBES):
            with self.subTest(probe=probe):
                fake = _FakeLinuxSyscalls(self.receipt)
                table = fake.table()
                original = table['backend_denials']

                def allowed(pid, pidfd, original=original, probe=probe):
                    value = original(pid, pidfd)
                    value[probe] = 'allowed'
                    return value

                table['backend_denials'] = allowed
                with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
                     patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        linux._installed_owner_inputs()
                self.assertEqual(got.exception.detail, 'peer_denied')

    def test_unsafe_peer_capabilities_should_refuse(self):
        for capability in ('effective', 'permitted', 'inheritable', 'ambient'):
            with self.subTest(capability=capability):
                fake = _FakeLinuxSyscalls(self.receipt)

                def violate(table, capability=capability):
                    process_snapshot = table['process_snapshot']

                    def privileged(pid, pidfd):
                        value = process_snapshot(pid, pidfd)
                        if pid == self.receipt['peer_process']['pid']:
                            value['capabilities'][capability] = 1
                        return value

                    table['process_snapshot'] = privileged

                with self.assertRaises(owner.OwnerRefusal) as got:
                    self.propose(fake, violate)
                self.assertEqual(got.exception.detail, 'peer_denied')

    def test_peer_without_no_new_privs_should_refuse(self):
        fake = _FakeLinuxSyscalls(self.receipt)

        def violate(table):
            process_snapshot = table['process_snapshot']

            def privileged(pid, pidfd):
                value = process_snapshot(pid, pidfd)
                if pid == self.receipt['peer_process']['pid']:
                    value['no_new_privs'] = False
                return value

            table['process_snapshot'] = privileged

        with self.assertRaises(owner.OwnerRefusal) as got:
            self.propose(fake, violate)
        self.assertEqual(got.exception.detail, 'peer_denied')

    def test_cleanup_should_try_both_descriptors_after_first_close_error(self):
        fake = _FakeLinuxSyscalls(self.receipt)
        table = fake.table()
        closed = []

        def close(fd):
            closed.append(fd)
            if fd == 702:
                raise OSError(errno.EIO, 'synthetic first close error')
            if fd == 701:
                raise OSError(errno.EBADF, 'synthetic second close error')

        table['pidfd_close'] = close
        with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
             patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
            with self.assertRaises(owner.OwnerRefusal) as got:
                linux._installed_linux_inputs()
        self.assertEqual(got.exception.detail, 'required_binding_gap')
        self.assertEqual(closed, [702, 701])
        self.assertIsInstance(got.exception.__cause__, OSError)
        self.assertEqual(got.exception.__cause__.errno, errno.EIO)

    def test_cleanup_closes_duplicate_pidfd_only_once(self):
        fake = _FakeLinuxSyscalls(self.receipt)
        table = fake.table()
        closed = []
        table['pidfd_open'] = lambda pid: 701
        table['pidfd_close'] = closed.append
        with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
             patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
            with self.assertRaises(owner.OwnerRefusal) as got:
                linux._installed_linux_inputs()
        self.assertEqual(got.exception.detail, 'required_binding_gap')
        self.assertEqual(closed, [701])

    def test_cleanup_close_error_does_not_replace_first_collector_refusal(self):
        fake = _FakeLinuxSyscalls(self.receipt)
        table = fake.table()
        closed = []

        def close(fd):
            closed.append(fd)
            if fd == 702:
                raise OSError(errno.EIO, 'synthetic close error after refusal')

        table['pidfd_alive'] = lambda fd: False
        table['pidfd_close'] = close
        with patch.object(linux, '_LINUX_BINDING', {'inputs': self.base, 'peer_fd': 700}, create=True), \
             patch.object(linux, '_LINUX_SYSCALLS', table, create=True):
            with self.assertRaises(owner.OwnerRefusal) as got:
                linux._installed_linux_inputs()
        self.assertEqual(got.exception.detail, 'peer_denied')
        self.assertEqual(closed, [702, 701])


class LinuxObjectSnapshotACLTests(unittest.TestCase):
    def _snapshot(self, *, custody_changed=False, acl_value=None):
        before = SimpleNamespace(st_dev=17, st_ino=23, st_mode=stat.S_IFSOCK | 0o660,
                                 st_uid=501, st_gid=20, st_nlink=1)
        after = SimpleNamespace(st_dev=17, st_ino=24 if custody_changed else 23,
                                st_mode=before.st_mode, st_uid=501, st_gid=20, st_nlink=1)
        stat_calls = []
        open_calls = []
        close_calls = []
        getxattr_calls = []

        def fake_open(path, flags, *, dir_fd=None):
            open_calls.append((path, flags, dir_fd))
            return 31 if path == '/' else 32

        def fake_stat(path, *, dir_fd=None, follow_symlinks=True):
            stat_calls.append((path, dir_fd, follow_symlinks))
            return before if len(stat_calls) == 1 else after

        def fake_fstat(fd):
            self.assertEqual(fd, 32)
            return before

        def fake_getxattr(path, name):
            getxattr_calls.append((path, name))
            if path == 32:
                raise OSError(errno.EBADF, 'synthetic O_PATH xattr refusal')
            if path == '/proc/self/fd/32':
                if acl_value is None:
                    raise OSError(errno.ENODATA, 'synthetic absent ACL')
                return acl_value
            raise AssertionError(f'unexpected xattr path: {path!r}')

        with patch.object(linux.os, 'open', side_effect=fake_open), \
             patch.object(linux.os, 'stat', side_effect=fake_stat), \
             patch.object(linux.os, 'fstat', side_effect=fake_fstat), \
             patch.object(linux.os, 'close', side_effect=close_calls.append), \
             patch.object(linux.os, 'getxattr', side_effect=fake_getxattr, create=True), \
             patch.object(linux.os, 'O_PATH', 0x200000, create=True), \
             patch.object(linux, '_fd_mount_id', return_value=29):
            result = linux._object_snapshot('/endpoint')
        return result, open_calls, close_calls, getxattr_calls

    def test_mode_only_socket_accepts_absent_acl_after_opath_retry(self):
        result, open_calls, close_calls, getxattr_calls = self._snapshot()
        self.assertEqual(result['kind'], 'socket')
        self.assertEqual(result['mode'], 0o660)
        self.assertEqual(result['acl'], [])
        self.assertEqual(result['identity'], [17, 23, 29])
        self.assertEqual(result['held_identity'], result['identity'])
        self.assertEqual([call[0] for call in getxattr_calls], [32, '/proc/self/fd/32'])
        self.assertTrue(open_calls[1][1] & 0x200000)
        self.assertEqual(close_calls, [32, 31])

    def test_valid_acl_bytes_still_parse_after_opath_retry(self):
        acl = struct.pack('<IHHI', 2, 1, 6, 0)
        result, _, _, _ = self._snapshot(acl_value=acl)
        self.assertEqual(result['acl'], [{'tag': 'owner', 'id': None, 'perm': 6}])

    def test_real_named_held_identity_change_refuses_as_custody_changed(self):
        with self.assertRaises(owner.OwnerRefusal) as got:
            self._snapshot(custody_changed=True)
        self.assertEqual(got.exception.detail, 'custody_changed')




class LinuxGroupAHostFit(unittest.TestCase):
    """Kai 16:38 source-only host profile; observations are not host authority."""
    profile = dict(os='linux', distribution='rocky', version='10.2', architecture='x86_64',
                   kernel='6.12.0-211.16.1.el10_2', selinux='enforcing')

    def binding(self):
        inputs = LinuxObserverOracle().inputs('positive')
        context = owner._decode(inputs.context_raw, 'owner_unavailable')
        peer = owner._original(inputs, context['peer_ref'])
        receipt = owner._original(inputs, peer['linux_ref'])
        fake = _FakeLinuxSyscalls(receipt)
        calls = fake.table()
        calls['host_snapshot'] = lambda: copy.deepcopy(self.profile)
        return inputs, fake, calls

    def collect(self, inputs, calls):
        with patch.object(linux, '_LINUX_BINDING', {'inputs': inputs, 'peer_fd': 700}), \
             patch.object(linux, '_LINUX_SYSCALLS', calls):
            return linux._installed_linux_inputs()

    def native(self):
        method = getattr(linux, '_host_snapshot', None)
        self.assertTrue(callable(method), 'GA_HOST_SURFACE_ABSENT: bounded native host observation')
        return method

    def test_missing_original_binding_refuses_before_host_observation(self):
        observed = []
        with patch.object(linux, '_LINUX_BINDING', None), \
             patch.object(linux, '_LINUX_SYSCALLS', {'host_snapshot': lambda: observed.append('host')}):
            with self.assertRaises(owner.OwnerRefusal) as caught:
                linux._installed_linux_inputs()
        self.assertEqual(caught.exception.detail, 'required_binding_gap')
        self.assertEqual(observed, [])

    def test_expected_profile_is_measured_before_existing_peer_collection(self):
        inputs, fake, calls = self.binding()
        def host():
            fake.events.append(('host_snapshot',))
            return copy.deepcopy(self.profile)
        calls['host_snapshot'] = host
        result = self.collect(inputs, calls)
        self.assertIs(type(result), owner._Inputs)
        self.assertEqual(fake.events[0], ('host_snapshot',))
        self.assertIn(('peer_pidfd', 700), fake.events)

    def test_each_incompatible_profile_refuses_before_peer_or_pidfd(self):
        for key, value in [('os','darwin'), ('distribution','ubuntu'), ('version','10.1'),
                           ('architecture','aarch64'), ('kernel','6.12.0-other'),
                           ('selinux','permissive')]:
            with self.subTest(key=key):
                inputs, fake, calls = self.binding()
                bad = dict(self.profile, **{key:value})
                calls['host_snapshot'] = lambda: bad
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    self.collect(inputs, calls)
                self.assertEqual(caught.exception.detail, 'required_binding_gap')
                self.assertEqual(fake.events, [])

    def test_missing_extra_and_substituted_profile_types_refuse(self):
        class Equal:
            def __eq__(self, other): return True
        bad = [None, dict(self.profile, extra=True),
               {k:v for k,v in self.profile.items() if k!='kernel'},
               dict(self.profile, kernel=Equal()), dict(self.profile, selinux=True)]
        for observation in bad:
            with self.subTest(shape=type(observation).__name__):
                inputs, fake, calls = self.binding()
                calls['host_snapshot'] = lambda: observation
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    self.collect(inputs, calls)
                self.assertEqual(caught.exception.detail, 'required_binding_gap')
                self.assertEqual(fake.events, [])

    def test_unavailable_host_observation_never_collects_peer(self):
        inputs, fake, calls = self.binding()
        def unavailable(): raise OSError(errno.EACCES, 'protected host unavailable')
        calls['host_snapshot'] = unavailable
        with self.assertRaises(owner.OwnerRefusal) as caught:
            self.collect(inputs, calls)
        self.assertEqual(caught.exception.detail, 'required_binding_gap')
        self.assertEqual(fake.events, [])

    def test_non_linux_native_probe_refuses_before_reading_any_file(self):
        probe = self.native()
        with patch.object(sys, 'platform', 'darwin'), \
             patch.object(linux.os, 'open', side_effect=AssertionError('native IO on wrong host')):
            with self.assertRaises(owner.OwnerRefusal) as caught: probe()
        self.assertEqual(caught.exception.detail, 'required_binding_gap')

    def test_native_profile_uses_os_release_uname_and_bounded_enforce_read(self):
        probe = self.native()
        with patch.object(sys, 'platform', 'linux'), \
             patch('platform.freedesktop_os_release', return_value={'ID':'rocky','VERSION_ID':'10.2'}), \
             patch.object(linux.os, 'uname', return_value=SimpleNamespace(release=self.profile['kernel'], machine='x86_64')), \
             patch.object(linux.os, 'open', return_value=901) as opened, \
             patch.object(linux, '_read_fd', return_value=b'1\n') as read, \
             patch.object(linux.os, 'close') as close:
            self.assertEqual(probe(), self.profile)
        opened.assert_called_once_with('/sys/fs/selinux/enforce',
            linux.os.O_RDONLY | linux.os.O_CLOEXEC | linux.os.O_NOFOLLOW)
        read.assert_called_once_with(901, 2)
        close.assert_called_once_with(901)

    def test_native_unavailable_or_non_enforcing_selinux_refuses_and_closes(self):
        probe = self.native()
        for raw in [b'0', b'', b'2', b'1\nextra']:
            with self.subTest(raw_length=len(raw)), patch.object(sys, 'platform', 'linux'), \
                 patch('platform.freedesktop_os_release', return_value={'ID':'rocky','VERSION_ID':'10.2'}), \
                 patch.object(linux.os, 'uname', return_value=SimpleNamespace(release=self.profile['kernel'], machine='x86_64')), \
                 patch.object(linux.os, 'open', return_value=901), \
                 patch.object(linux, '_read_fd', return_value=raw), \
                 patch.object(linux.os, 'close') as close:
                with self.assertRaises(owner.OwnerRefusal): probe()
                close.assert_called_once_with(901)


if __name__ == "__main__":
    unittest.main()
