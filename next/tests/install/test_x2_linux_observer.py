"""X2 Linux owner-binding fixture contract; no host or native qualification."""
import copy
import sys
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.install.platforms import linux
from test_gate_b_linux import LinuxObserverOracle


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


if __name__ == "__main__":
    unittest.main()
