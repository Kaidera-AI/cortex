"""BL literal SOURCE contract oracle; no kernel, protected-V or effect credit.

For the original 86-method oracle, 83 methods preceded linux.py, three regressions were added later with staged RED receipts, and all 86 have separate source-absence BODY RED on both interpreters.
The only supplier substituted here is private and test-local. No signer, actual
collector, native namespace, host path, account, process or service is used.
"""
import ast
import copy
import importlib
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble

NEXT = Path(__file__).resolve().parents[2]
SUBJECTS = json.loads((Path(__file__).parent/'fixtures/gate-b-linux-pgc20-v1.json').read_bytes())


class LinuxObserverOracle(unittest.TestCase):
    def api(self):
        self.assertTrue((NEXT/'src/cortex_core/install/platforms/linux.py').is_file(),
                        'BL_SOURCE_SURFACE_ABSENT: fixed Linux observer')
        return importlib.import_module('cortex_core.install.platforms.linux')

    def inputs(self, name):
        d = SUBJECTS['cases'][name]['inputs']
        return owner._Inputs(d['context_raw'].encode(), {k:v.encode() for k,v in d['originals'].items()},
                             d['current_clock_raw'].encode(), d['aggregate_id'])

    def observe(self, api, name):
        original = self.inputs(name)
        counts = dict(observations=0, reservations=0, consumptions=0, primitives=0)
        # Zero authority counters measure no available effect entry in this read-only
        # source composition. OS/network/process attempts are independently denied by
        # the runner audit; they are not represented as native kernel measurements.
        with patch.object(api, '_installed_linux_inputs', return_value=original):
            try:
                result = api._installed_owner_inputs()
            except owner.OwnerRefusal as e:
                outcome = dict(detail=e.detail, result=e.result, reason=e.reason_code, **counts)
            else:
                self.assertIs(result, original)
                self.assertIs(type(result), owner._Inputs)
                counts['observations'] += 1
                outcome = dict(detail=None, result='DATA', reason=None, **counts)
        return outcome

    def check_case(self, name):
        api = self.api()
        self.assertEqual(self.observe(api, 'positive'), SUBJECTS['cases']['positive']['expected'])
        self.assertEqual(self.observe(api, name), SUBJECTS['cases'][name]['expected'],
                         'BL_DECIDING_LITERAL:'+name)

    def join(self, api, fresh='positive'):
        values = [self.inputs('positive'), self.inputs(fresh)]
        events = []; proposals = []; admitted = []
        def collect():
            events.append('observe')
            return values[len([x for x in events if x == 'observe'])-1]
        double = NonAuthoritativeSignedMemberDouble(SUBJECTS['runtime_lock_raw'].encode(),
            aggregate_id=values[0].aggregate_id, member_name='runtime.lock.json')
        read = double.read_original
        def audited_read(limit):
            events.append('r01-read')
            return read(limit)
        constructor = owner.OwnerProposal
        def counted_proposal(*args, **kwargs):
            value = constructor(*args, **kwargs); proposals.append(value); return value
        with patch.object(api, '_installed_linux_inputs', collect), \
             patch.object(owner, '_installed_owner_inputs', api._installed_owner_inputs), \
             patch.object(owner, 'OwnerProposal', counted_proposal), \
             patch.object(double, 'read_original', audited_read), double.installed(signed_member):
            try:
                value = owner.propose_effect(SUBJECTS['request_raw'].encode())
            except owner.OwnerRefusal as e:
                refusal = (e.detail, e.result, e.reason_code)
            else:
                refusal = None; admitted.append(value)
        return refusal, admitted, proposals, events, double

    def test_surface_uses_existing_private_zero_argument_carrier(self):
        api = self.api()
        self.assertEqual(list(inspect.signature(api._installed_owner_inputs).parameters), [])
        self.assertEqual(list(inspect.signature(api._installed_linux_inputs).parameters), [])
        self.assertEqual(api.__all__, [])
        source = ast.parse((NEXT/'src/cortex_core/install/platforms/linux.py').read_bytes())
        self.assertFalse([n for n in ast.walk(source) if isinstance(n, ast.ClassDef)])
        self.assertFalse([n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and not n.name.startswith('_')])
        for args in ((b'caller',), ('/caller/root',), (lambda: None,)):
            with self.assertRaises(TypeError): api._installed_owner_inputs(*args)

    def test_default_gap_before_candidate_io_or_effect(self):
        api = self.api()
        class Trap:
            def __getattribute__(self, name): raise AssertionError('candidate touched')
        def forbidden(*args, **kwargs): self.fail('unbound source attempted IO or effect')
        with patch.object(owner, '_installed_owner_inputs', api._installed_owner_inputs), \
             patch.object(Path, 'read_bytes', forbidden), patch('builtins.open', forbidden), \
             patch('socket.socket', forbidden), patch('subprocess.Popen', forbidden):
            with self.assertRaises(owner.OwnerRefusal) as got: owner.propose_effect(Trap())
        self.assertEqual((got.exception.detail, got.exception.result, got.exception.reason_code,
                          got.exception.unit), ('required_binding_gap','BLOCKED','dependency_not_run',owner.MISSING_UNIT))

    def test_common_production_collector_remains_unbound(self):
        self.api()
        with self.assertRaises(owner.OwnerRefusal) as got: owner._installed_owner_inputs()
        self.assertEqual(got.exception.detail, 'required_binding_gap')

    def test_existing_original_ref_and_decoder_reject_missing_digest_duplicate(self):
        for name in ('reference_digest','missing_original','duplicate_json_key'):
            with self.subTest(case=name): self.check_case(name)

    def test_b1_join_reobserves_after_sole_r01_read_and_is_data(self):
        api = self.api()
        refusal, values, proposals, events, double = self.join(api)
        self.assertIsNone(refusal)
        self.assertEqual(len(values), 1)
        self.assertEqual(events, ['observe','r01-read','observe'])
        # B1 creates an internal first-evaluation DATA candidate and the fresh one.
        self.assertEqual(len(proposals), 2)
        self.assertEqual(len(double.calls), 1); self.assertEqual(len(double.reads), 1)
        value = values[0]
        self.assertEqual(value.stage, 'PRECONSUME_DATA')
        self.assertFalse(value.effect_authority); self.assertFalse(value.native_qualified)
        self.assertFalse(value.reservation_created); self.assertFalse(value.consumed)
        self.assertEqual((value.deadline.expires_at, value.deadline.expires_monotonic_ns),
                         (1700000005,105000000000))

    def test_b1_fresh_linux_negatives_prevent_return_after_long_audit(self):
        api = self.api()
        for name, case in SUBJECTS['cases'].items():
            if case['expected']['detail'] is None: continue
            with self.subTest(case=name):
                refusal, values, proposals, events, double = self.join(api, name)
                self.assertEqual(refusal, (case['expected']['detail'], case['expected']['result'],
                                           case['expected']['reason']))
                self.assertEqual(values, [])
                # Only the pre-audit DATA candidate exists; no fresh proposal escapes.
                self.assertEqual(len(proposals), 1)
                self.assertEqual(events, ['observe','r01-read','observe'])
                self.assertEqual(len(double.calls), 1); self.assertEqual(len(double.reads), 1)
                self.assertFalse(proposals[0].effect_authority)
                self.assertFalse(proposals[0].reservation_created)
                self.assertFalse(proposals[0].consumed)

    def test_fixture_is_explicitly_simulated_without_authority(self):
        self.api()
        self.assertEqual(SUBJECTS['scope'], 'SIMULATED_SOURCE_CONTRACT_ONLY_NO_NATIVE_PROVENANCE_OR_AUTHORITY')
        self.assertEqual(SUBJECTS['authority'], dict(protected_collector='UNBOUND',reservations=0,
                                                   consumptions=0,primitives=0))

    def test_literal_positive(self):
        self.check_case('positive')

    def test_literal_positive_80_object_closure(self):
        self.check_case('positive_80_object_closure')

    def test_literal_positive_280_object_closure(self):
        self.check_case('positive_280_object_closure')

    def test_literal_shape(self):
        self.check_case('shape')

    def test_literal_binding(self):
        self.check_case('binding')

    def test_literal_transport(self):
        self.check_case('transport')

    def test_literal_connection(self):
        self.check_case('connection')

    def test_literal_peer_lifetime(self):
        self.check_case('peer_lifetime')

    def test_literal_peer_credentials(self):
        self.check_case('peer_credentials')

    def test_literal_owner_separation(self):
        self.check_case('owner_separation')

    def test_literal_owner_lifetime(self):
        self.check_case('owner_lifetime')

    def test_literal_namespaces(self):
        self.check_case('namespaces')

    def test_literal_map_policy(self):
        self.check_case('map_policy')

    def test_literal_map_identity(self):
        self.check_case('map_identity')

    def test_literal_capabilities(self):
        self.check_case('capabilities')

    def test_literal_privilege_paths(self):
        self.check_case('privilege_paths')

    def test_literal_owner_code(self):
        self.check_case('owner_code')

    def test_literal_startup(self):
        self.check_case('startup')

    def test_literal_catalog(self):
        self.check_case('catalog')

    def test_literal_lexical_path(self):
        self.check_case('lexical_path')

    def test_literal_ancestors(self):
        self.check_case('ancestors')

    def test_literal_role_kind(self):
        self.check_case('role_kind')

    def test_literal_unknown_role(self):
        self.check_case('unknown_role')

    def test_literal_object_kind(self):
        self.check_case('object_kind')

    def test_literal_object_identity(self):
        self.check_case('object_identity')

    def test_literal_held_mount_id(self):
        self.check_case('held_mount_id')

    def test_literal_policy_digest(self):
        self.check_case('policy_digest')

    def test_literal_state_digest(self):
        self.check_case('state_digest')

    def test_literal_object_digest(self):
        self.check_case('object_digest')

    def test_literal_object_owner(self):
        self.check_case('object_owner')

    def test_literal_special_modes(self):
        self.check_case('special_modes')

    def test_literal_acl_shape(self):
        self.check_case('acl_shape')

    def test_literal_caller_write(self):
        self.check_case('caller_write')

    def test_literal_default_acl(self):
        self.check_case('default_acl')

    def test_literal_endpoint_access(self):
        self.check_case('endpoint_access')

    def test_literal_peer_code(self):
        self.check_case('peer_code')

    def test_literal_abstract_socket(self):
        self.check_case('abstract_socket')

    def test_literal_later_pid_lookup(self):
        self.check_case('later_pid_lookup')

    def test_literal_saved_uid(self):
        self.check_case('saved_uid')

    def test_literal_fs_gid(self):
        self.check_case('fs_gid')

    def test_literal_extra_group(self):
        self.check_case('extra_group')

    def test_literal_no_new_privs(self):
        self.check_case('no_new_privs')

    def test_literal_privilege_ptrace(self):
        self.check_case('privilege_ptrace')

    def test_literal_privilege_proc_mem(self):
        self.check_case('privilege_proc_mem')

    def test_literal_privilege_process_vm_writev(self):
        self.check_case('privilege_process_vm_writev')

    def test_literal_privilege_setns(self):
        self.check_case('privilege_setns')

    def test_literal_privilege_setuid(self):
        self.check_case('privilege_setuid')

    def test_literal_privilege_file_capability(self):
        self.check_case('privilege_file_capability')

    def test_literal_privilege_launch_write(self):
        self.check_case('privilege_launch_write')

    def test_literal_privilege_backend_bypass(self):
        self.check_case('privilege_backend_bypass')

    def test_literal_privilege_lifetime(self):
        self.check_case('privilege_lifetime')

    def test_literal_owner_namespace(self):
        self.check_case('owner_namespace')

    def test_literal_overlapping_map(self):
        self.check_case('overlapping_map')

    def test_literal_missing_namespace(self):
        self.check_case('missing_namespace')

    def test_literal_bool_uid(self):
        self.check_case('bool_uid')

    def test_literal_negative_identity(self):
        self.check_case('negative_identity')

    def test_literal_pth(self):
        self.check_case('pth')

    def test_literal_site(self):
        self.check_case('site')

    def test_literal_wrong_crypto(self):
        self.check_case('wrong_crypto')

    def test_literal_unlisted_import(self):
        self.check_case('unlisted_import')

    def test_literal_missing_native(self):
        self.check_case('missing_native')

    def test_literal_hardlink(self):
        self.check_case('hardlink')

    def test_literal_named_inode(self):
        self.check_case('named_inode')

    def test_literal_named_mount_id(self):
        self.check_case('named_mount_id')

    def test_literal_parent_write(self):
        self.check_case('parent_write')

    def test_literal_parent_no_search(self):
        self.check_case('parent_no_search')

    def test_literal_acl_write(self):
        self.check_case('acl_write')

    def test_literal_positive_acl_mask(self):
        self.check_case('positive_acl_mask')

    def test_literal_positive_named_user_no_fallback(self):
        self.check_case('positive_named_user_no_fallback')

    def test_literal_positive_group_no_fallback(self):
        self.check_case('positive_group_no_fallback')

    def test_literal_acl_group_union(self):
        self.check_case('acl_group_union')

    def test_literal_peer_before_custody(self):
        self.check_case('peer_before_custody')

    def test_literal_peer_before_state_unavailable(self):
        self.check_case('peer_before_state_unavailable')

    def test_literal_linux_target(self):
        self.check_case('linux_target')

    def test_literal_boot_binding(self):
        self.check_case('boot_binding')

    def test_literal_launch_write(self):
        self.check_case('launch_write')

    def test_literal_carrier_bounds(self):
        self.check_case('carrier_bounds')

    def test_literal_context_shape(self):
        self.check_case('context_shape')

    def test_literal_reference_digest(self):
        self.check_case('reference_digest')

    def test_literal_missing_original(self):
        self.check_case('missing_original')

    def test_literal_duplicate_json_key(self):
        self.check_case('duplicate_json_key')
