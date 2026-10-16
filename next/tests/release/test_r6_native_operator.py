"""R6 native operator: probe asset, frozen outcome table, originals, order, lifetime, packages.

Accepted note v2 + kai's three build clauses (PID cap attribution, five lane windows,
exactly-our-transaction package ownership). Fixture level only: fake engines stand in
at the existing seams (runner._child, inspector._engine); no host, container, unit,
dnf or native run. Nothing here is native or containment evidence; the runtime class
stays MISSING (Cox smoke contract), so no lane PASS is claimed.
"""
import contextlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_r3_inspector as r3
import test_r4_native_lane as r4
from test_r4_native_lane import (ALLOWLIST, HOST, Engine, Lane, codec, contracts, inspector, observations, originals,
                                 pending, probe_closure, product, qualification, receipts, runner, verified)

FAIL = 'native_inspector_execution_unqualified'
SESSION = '/fixture/owned/session-' + 'a' * 32
NAME = 'cortex-inspector-' + 'b' * 32


def context(lane, doc, files, rows):
    value = {'capability': lane.closure, 'probe': lane.probe,
             'descriptor': contracts.thaw(lane.closure.descriptor)['descriptor'], 'profile': lane.fx.profile,
             'input_value': lane.value, 'doc': doc, 'allowlist': ALLOWLIST, 'positive': None}
    value['positive'] = json.loads(files[rows['positive']['request']])
    return value


def row(lane, kind, files=None, rows=None, **engine):
    """qualification_row on ONE row of freshly collected originals (the shared R01 function)."""
    if files is None:
        files, rows = originals(lane, **engine)
    doc = qualification(lane, files={})
    cited = rows[kind]
    children = json.loads(files[cited['children']])
    return receipts.qualification_row(kind, files[cited['request']], children, files, context(lane, doc, files, rows))


class Base(unittest.TestCase):
    def qualifies(self, lane, kind, **engine):
        streams, _ = row(lane, kind, **engine)
        self.assertTrue(streams)

    def refuses(self, fn, *args, reason=FAIL, **kw):
        with self.assertRaises(ValueError) as caught:
            fn(*args, **kw)
        self.assertEqual(str(caught.exception), reason)


class Asset(Base):
    """R6-ASSET-001: the probe asset goes through the SAME port and can never be the
    inspector, and no session path accepts it."""

    def test_probe_asset_is_admitted_as_purpose_probe_by_the_same_port(self):
        lane = Lane(self)
        probe = probe_closure(lane)
        self.assertEqual(contracts.thaw(probe.descriptor)['purpose'], 'probe')
        self.assertEqual(contracts.thaw(lane.closure.descriptor)['purpose'], 'inspector')
        self.assertEqual((probe.engine, probe.engine_sha256), (lane.closure.engine, lane.closure.engine_sha256))
        self.assertNotEqual(probe.image_digest, lane.closure.image_digest)
        limits = lane.fx.profile['inspector']['limits']
        plans = [list(inspector.plan_argv(str(c.engine), c.image_digest, limits, SESSION, NAME)) for c in (probe, lane.closure)]
        self.assertEqual([i for i, (a, b) in enumerate(zip(*plans)) if a != b], [plans[0].index(probe.image_digest)])

    def test_changed_probe_script_or_the_real_inspector_as_probe_refuses(self):
        lane = Lane(self)
        probe_closure(lane)
        descriptor = contracts.thaw(lane.probe.descriptor)['descriptor']
        raw = codec.canonical_bytes(descriptor)
        admit = lambda script: inspector.admit_closure(raw, r4.sha(raw), lane.fx.profile, lane.fx.top / 'probe-owned',
                                                       candidate_roots=[lane.fx.candidate], script=script)
        self.refuses(admit, b'# another probe\n', reason='inspector_image_member_unbound')
        self.refuses(admit, r3.SCRIPT, reason='inspector_image_member_unbound')
        self.assertEqual(contracts.thaw(admit(Path(product('_probe').__file__).read_bytes()).descriptor)['purpose'], 'probe')

    def test_probe_closure_never_reaches_a_session_or_an_inspector_row(self):
        lane = Lane(self)
        probe = probe_closure(lane)
        self.refuses(inspector._session, probe, lane.fx.profile, reason='trusted_inspector_capability_unavailable')
        self.refuses(inspector.collect_inspection_evidence, probe, lane.fx.profile, r3.rows(('a', b'x = 1\n')),
                     reason='inspector_capability_untrusted')
        self.refuses(inspector.run_probe, lane.closure, lane.fx.profile, 'network', reason='inspector_capability_untrusted')
        self.refuses(inspector.preflight, verified(lane.fx, lane.value), {'inspector_capability': probe, 'inspector_host': HOST},
                     reason='trusted_inspector_capability_unavailable')

    def test_probe_digest_without_its_loaded_asset_refuses(self):
        lane = Lane(self)
        probe_closure(lane)
        self.refuses(row, lane, 'network', image_ids={lane.probe.image_digest: 'e' * 64})
        self.qualifies(lane, 'network')

    def test_acceptance_without_a_matching_probe_or_allowlist_leaves_no_probe_context(self):
        lane = Lane(self)
        probe = probe_closure(lane)
        checked = verified(lane.fx, lane.value)
        base = {'inspector_capability': lane.closure, 'inspector_host': HOST}
        full = inspector.preflight(checked, dict(base, probe_capability=probe, containment_allowlist=ALLOWLIST))
        self.assertIs(full.probe, probe)
        self.assertEqual(contracts.thaw(full.allowlist), ALLOWLIST)
        for change in ({'probe_capability': lane.closure}, {'containment_allowlist': {'mounts': ['/']}}, {}):
            with self.subTest(change=sorted(change)):
                got = inspector.preflight(checked, dict(base, **change))
                self.assertTrue(got.probe is None or got.allowlist is None)


class Outcomes(Base):
    """R6-ORACLE-001 = R6-T2: ONE frozen table, each authentic row paired with its
    refusing restore; child success is never row success."""

    def test_pids_qualifies_only_on_an_attributed_denial_at_the_admitted_cap(self):
        lane = Lane(self)
        cap = lane.fx.profile['inspector']['limits']['pids']
        authentic = observations(lane)['pids']
        self.assertEqual((authentic['pids_max'], authentic['pids_current_at_denial'], authentic['errno']), (cap, cap, 'EAGAIN'))
        self.qualifies(lane, 'pids')
        bad = {'early EAGAIN below the cap': dict(authentic, pids_current_at_denial=cap - 3),
               'unattributed EAGAIN (no pids.events max)': dict(authentic, pids_events_max_delta=0),
               'other cap than the plan': dict(authentic, pids_max=cap + 16, pids_current_at_denial=cap + 16),
               'no denial': dict(authentic, errno='NO_DENIAL', pids_current_at_denial=None),
               'children not reaped': dict(authentic, reaped=False)}
        for name, value in bad.items():
            with self.subTest(case=name):
                self.refuses(row, lane, 'pids', observe={'pids': value})

    def test_network_needs_only_lo_and_no_route(self):
        lane = Lane(self)
        good = observations(lane)['network']
        self.qualifies(lane, 'network')
        for name, value in {'refused': dict(good, errno='ECONNREFUSED'), 'timeout': dict(good, errno='ETIMEDOUT'),
                            'no attempt': dict(good, attempted=None), 'an interface': dict(good, interfaces=['eth0', 'lo'])}.items():
            with self.subTest(case=name):
                self.refuses(row, lane, 'network', observe={'network': value})

    def test_rootfs_and_tmpfs_need_the_read_only_cause(self):
        lane = Lane(self)
        good = observations(lane)
        self.qualifies(lane, 'rootfs_write')
        self.qualifies(lane, 'tmpfs')
        for kind, value in (('rootfs_write', dict(good['rootfs_write'], errno='EACCES')),
                            ('rootfs_write', dict(good['rootfs_write'], errno='WRITTEN', written=True)),
                            ('tmpfs', dict(good['tmpfs'], tmpfs_on_tmp=True)),
                            ('tmpfs', dict(good['tmpfs'], errno='EACCES'))):
            with self.subTest(kind=kind, value=value['errno']):
                self.refuses(row, lane, kind, observe={kind: value})

    def test_memory_needs_the_cgroup_oom_kill_not_a_bare_137(self):
        lane = Lane(self)
        good = observations(lane)['memory']
        self.qualifies(lane, 'memory')
        self.refuses(row, lane, 'memory', run_exit={'memory': 137})
        for name, value in {'no oom_kill +1': dict(good, oom_kill_delta=0), 'other limit': dict(good, memory_max=1 << 30),
                            'not over the limit': dict(good, attempted_bytes=good['memory_max']),
                            'other signal': dict(good, child_signal=15)}.items():
            with self.subTest(case=name):
                self.refuses(row, lane, 'memory', observe={'memory': value})

    def test_time_needs_the_admitted_deadline_and_absence(self):
        lane = Lane(self)
        self.qualifies(lane, 'time')
        self.refuses(row, lane, 'time', time_mode='early')
        self.refuses(row, lane, 'time', survivor='time')
        files, rows = originals(lane)
        children = json.loads(files[rows['time']['children']])
        children[2]['timeout_ns'] //= 2  # a deadline that is not the plan's limits.seconds
        wrong = dict(files, **{rows['time']['children']: codec.canonical_bytes(children)})
        self.refuses(row, lane, 'time', files=wrong, rows=rows)

    def test_mounts_and_environment_equal_the_accepted_allowlist(self):
        lane = Lane(self)
        good = observations(lane)['mounts_env']
        self.qualifies(lane, 'mounts_env')
        for name, value in {'extra mount': dict(good, mounts=sorted(good['mounts'] + ['/host'])),
                            'extra variable': dict(good, environment=sorted(good['environment'] + ['TOKEN']))}.items():
            with self.subTest(case=name):
                self.refuses(row, lane, 'mounts_env', observe={'mounts_env': value})

    def test_positive_and_negative_are_rederived_from_the_real_replies(self):
        lane = Lane(self)
        self.qualifies(lane, 'positive')
        self.qualifies(lane, 'negative')

        def unequal(reply, request):
            value = json.loads(reply)
            value['verdicts'][1]['equal'] = False
            return codec.canonical_bytes(value)
        self.refuses(row, lane, 'positive', reply=unequal)
        self.refuses(row, lane, 'negative', negative_same=True)
        self.refuses(row, lane, 'negative', reply=unequal)  # two false verdicts, not exactly the flipped one

    def test_every_probe_row_fails_without_its_cleanup_absence_evidence(self):
        lane = Lane(self)
        for kind in receipts.PROBES:
            with self.subTest(kind=kind):
                self.refuses(row, lane, kind, survivor=kind)


class Originals(Base):
    """R6-T1: every derivation input is a measured original bound to its plan; a changed
    one refuses even when the producer said pass, and restoring it restores the row."""

    def change(self, files, rows, kind, index, edit):
        files = dict(files)
        children = json.loads(files[rows[kind]['children']])
        edit(children[index], files)
        files[rows[kind]['children']] = codec.canonical_bytes(children)
        return files

    def test_tampered_originals_refuse_and_the_authentic_ones_restore(self):
        lane = Lane(self)
        files, rows = originals(lane)
        stdout = json.loads(files[rows['network']['children']])[2]['stdout']['path']
        cases = {
            'changed probe stdout': ('network', dict(files, **{stdout: files[stdout].replace(b'ENETUNREACH', b'ENETDOWN!!!')})),
            'absent reply stream': ('positive', {k: v for k, v in files.items()
                                                 if k != json.loads(files[rows['positive']['children']])[2]['stdout']['path']}),
            'wrong request/reply join': ('positive', dict(files, **{rows['positive']['request']: files[rows['negative']['request']]})),
            'cleanup for another --name': ('pids', self.change(files, rows, 'pids', 3, lambda c, f: c['argv'].__setitem__(
                -1, 'cortex-inspector-' + 'f' * 32))),
            'run from another image': ('positive', self.change(files, rows, 'positive', 2, lambda c, f: c.__setitem__(
                'argv', [lane.probe.image_digest if t == lane.closure.image_digest else t for t in c['argv']]))),
            'other limits': ('memory', self.change(files, rows, 'memory', 2, lambda c, f: c.__setitem__(
                'argv', [str(1 << 30) if t == str(1 << 28) else t for t in c['argv']]))),
            'other engine': ('tmpfs', self.change(files, rows, 'tmpfs', 1, lambda c, f: c['argv'].__setitem__(0, '/usr/bin/other')))}
        for name, (kind, tampered) in cases.items():
            with self.subTest(case=name):
                self.refuses(row, lane, kind, files=tampered, rows=rows)
                self.assertTrue(row(lane, kind, files=files, rows=rows)[0])

    def test_probe_evidence_can_never_fill_an_inspector_row(self):
        lane = Lane(self)
        files, rows = originals(lane)
        for kind in receipts.PROBES:
            with self.subTest(kind=kind):
                swapped = dict(rows, positive=rows[kind])
                self.refuses(row, lane, 'positive', files=files, rows=swapped)

    def test_the_record_carries_references_only_never_an_outcome(self):
        lane = Lane(self)
        doc = qualification(lane, files={})
        for extra in ({'result': 'PASS'}, {'containment': {**doc['containment'], 'pids': {**doc['containment']['pids'], 'ok': True}}}):
            with self.subTest(extra=sorted(extra)):
                self.refuses(receipts.qualification_complete, dict(doc, **extra), reason='native_qualification_incomplete')


class Order(Base):
    """R6-ORDER-001: P lane (corpus = positive) -> negative -> 7 probes -> record (row
    function ok) -> native_lanes(Core) -> native_lanes(API); nothing out of order."""

    def modules(self, lane):
        made = {}
        for name in ('Core', 'API'):
            out, ev = lane.fx.top / (name + '-outputs'), lane.fx.top / (name + '-evidence')
            out.mkdir(mode=0o700)
            ev.mkdir(mode=0o700)
            made[name] = {'verified': verified(lane.fx, lane.value), 'materialize': lane.materialize, 'base': lane.base,
                          'output_root': out, 'evidence_root': ev}
        return made

    def operate(self, lane, *, audit=True, smoke=None, **engine):
        probe = probe_closure(lane)
        wall = r4.Wall()
        fake = r4.QEngine(lane, wall, **engine)
        calls = []
        real = runner.native_lanes

        def lanes(*args, **kw):
            evidence = Path(args[7])
            calls.append((evidence.name, sorted(str(p.relative_to(evidence)) for p in evidence.rglob('*') if p.is_file())))
            return real(*args, **kw)

        def inspect(*args, inspection, **kwargs):
            if audit:
                inspection.check(r3.rows(('a', b'x = 1\n'), ('b', b'y = 2\n')))
            return (), [{'path': 'app/module.py', 'bytes': 5, 'sha256': '0' * 64, 'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
        root = lane.fx.top / 'qualification'
        root.mkdir(mode=0o700)
        smoke_patch = patch.object(runner, '_runtime_smoke', return_value=smoke) if smoke else contextlib.nullcontext()
        with patch.object(product('artifacts'), 'inspect_oci', inspect), patch.object(inspector, '_engine', fake), \
                patch.object(runner, '_child', Engine(lane, ps=[[]] * 12)), patch.object(runner, 'native_lanes', lanes), \
                patch.object(r4.time, 'time_ns', wall), smoke_patch:
            try:
                result = runner.operate(self.modules(lane), lane.closure, probe, HOST, lane.parent, root, ALLOWLIST)
            except ValueError as error:
                return None, calls, str(error)
        return result, calls, None

    def test_qualification_precedes_exactly_two_native_lanes_calls_core_then_api(self):
        lane = Lane(self)
        result, calls, error = self.operate(lane)
        self.assertIsNone(error)
        self.assertEqual([name for name, _ in calls], ['Core-evidence', 'API-evidence'])
        record = json.loads(result['record'])
        cited = set(receipts.qualification_paths(record))
        for _, present in calls:
            self.assertTrue(cited <= set(present))  # every original is in place BEFORE the lane call
        # runtime stays MISSING (Cox smoke contract): honest RED lanes, no RAW, no PASS
        self.assertEqual({name: value['result'] for name, value in result['lanes'].items()}, {'Core': 'RED', 'API': 'RED'})

    def test_a_refused_row_means_no_record_and_no_native_lanes_call(self):
        lane = Lane(self)
        _, calls, error = self.operate(lane, observe={'network': {**observations(lane)['network'], 'errno': 'ECONNREFUSED'}})
        self.assertEqual((error, calls), (FAIL, []))

    def test_missing_corpus_refuses_before_any_native_lanes_call(self):
        lane = Lane(self)
        _, calls, error = self.operate(lane, audit=False)
        self.assertEqual((error, calls), (FAIL, []))

    def test_p_lane_material_is_never_emitted(self):
        lane = Lane(self)
        result, _, error = self.operate(lane, smoke=('pass', 'fixture contract'))
        self.assertIsNone(error)
        for name in ('Core', 'API'):
            value = json.loads(result['lanes'][name]['receipt'])
            outputs = [r['path'] for slot in ('A', 'B') for r in value['builds'][slot]['outputs']]
            self.assertTrue(outputs and all(p.split('/')[0] in ('A', 'B') for p in outputs))
            originals_rows = [r for r in value['evidence']['inventory'] if r['role'] == 'native.qualification.original']
            self.assertTrue(all(r['path'].startswith(('q/', 'P/')) for r in originals_rows))
            build_rows = [r for r in value['evidence']['inventory'] if r['path'].startswith('P/')]
            self.assertEqual({r['role'] for r in build_rows}, {'native.qualification.original'})
            # the R01 verifier re-derives every row from these originals and mints ONE session
            measured = {r['path']: (lane.fx.top / (name + '-evidence') / r['path']).read_bytes()
                        for r in value['evidence']['inventory']}
            doc = json.loads(measured['qualification.json'])
            matched, found = receipts._match_qualification(doc, pending(lane), lane.value, value['builds'], measured,
                                                           value['evidence']['inventory'], 'd' * 64, 'e' * 64)
            self.assertEqual(set(found), {r['path'] for r in originals_rows})


class Lifetime(Base):
    """R6-LIFETIME-001 + kai: five lane windows, a reconciled total, one bounded unit,
    an always-run finally and always-attempted deallocation."""

    def test_budget_charges_five_lane_windows_and_reconciles(self):
        budget = runner.operator_budget(600)
        self.assertEqual(budget['stages'], {'setup': 1200, 'assets': 1200, 'qualification': 1800, 'lanes': 5 * 600,
                                            'teardown': 1200})
        self.assertEqual(budget['total_sec'], 1200 + 1200 + 1800 + 3000 + 1200)
        clock = r4.Clock()
        deadline = runner.operator_deadline(budget, clock=clock)
        self.assertEqual((deadline.expires, deadline.work_expires), (budget['total_sec'], budget['total_sec'] - 1200))
        for bad in ({**budget, 'total_sec': budget['total_sec'] - 1},
                    {'stages': {**budget['stages'], 'lanes': 4 * 600}, 'total_sec': budget['total_sec']},
                    {'stages': {**budget['stages'], 'teardown': 0}, 'total_sec': budget['total_sec'] - 1200}):
            self.refuses(runner.operator_deadline, bad, reason='operator_budget_unreconciled')
        for bad in (0, -1, 1.5, '600', True):
            self.refuses(runner.operator_budget, bad, reason='operator_budget_unreconciled')

    def test_unit_is_a_file_in_run_systemd_with_runtime_max_and_stop_post(self):
        budget = runner.operator_budget(600)
        path, text = runner.operator_unit('cortex-r6-amd64-1', budget, ['/usr/libexec/cortex/r6-run', '--arch=amd64'],
                                          ['/usr/libexec/cortex/r6-finally', '--arch=amd64'])
        self.assertEqual(path, '/run/systemd/system/cortex-r6-amd64-1.service')
        self.assertIn('RuntimeMaxSec=' + str(budget['total_sec']) + '\n', text)
        self.assertIn('ExecStopPost=/usr/libexec/cortex/r6-finally --arch=amd64\n', text)
        self.assertNotIn('systemd-run', text)
        for args in (('../escape', budget, ['/bin/true'], ['/bin/true']),
                     ('cortex-r6-x', {**budget, 'total_sec': 1}, ['/bin/true'], ['/bin/true']),
                     ('cortex-r6-x', budget, ['/bin/sh', '-c', 'rm -rf /'], ['/bin/true']),
                     ('cortex-r6-x', budget, ['relative'], ['/bin/true'])):
            self.refuses(runner.operator_unit, *args, reason='operator_unit_refused')

    def test_finally_attempts_every_step_and_keeps_failures(self):
        ran = []

        def step(name, outcome):
            def run():
                ran.append(name)
                if outcome == 'raise':
                    raise OSError('busy')
                return outcome
            return name, run
        residuals = runner.operator_finally([step('stop', True), step('absent', 'raise'), step('custody', False),
                                             step('user', True), step('packages', True)])
        self.assertEqual(ran, ['stop', 'absent', 'custody', 'user', 'packages'])
        self.assertEqual(residuals, ['absent: OSError', 'custody'])

    def drive(self, states, *, start=None, deallocate=None, readback='deallocated', polls=5):
        calls = {'start': 0, 'deallocate': 0}

        def do_start():
            calls['start'] += 1
            if start:
                raise start

        def do_deallocate():
            calls['deallocate'] += 1
            if deallocate:
                raise deallocate
        feed = iter(states)

        def read():
            value = next(feed)
            if isinstance(value, Exception):
                raise value
            return value
        result = runner.operator_drive(do_start, read, do_deallocate, lambda: readback, polls=polls)
        return result, calls

    def test_deallocation_is_always_requested_and_unknown_runs_are_never_duplicated(self):
        cases = {'clean': (['active', 'inactive'], {}, 'PASS'),
                 'deadline expiry (unit still running)': (['active'] * 5, {}, 'HOLD'),
                 'cleanup failed in the unit': (['active', 'failed'], {}, 'RED'),
                 'unknown state': (['active', 'gone?'], {}, 'HOLD'),
                 'lost connection': (['active', ConnectionError('ssh')], {}, 'HOLD'),
                 'start failed': ([], {'start': OSError('busy')}, 'RED'),
                 'deallocation unconfirmed': (['inactive'], {'readback': 'running'}, 'HOLD'),
                 'deallocate raised': (['inactive'], {'deallocate': OSError('api'), 'readback': 'running'}, 'HOLD')}
        for name, (states, options, expected) in cases.items():
            with self.subTest(case=name):
                result, calls = self.drive(states, **options)
                self.assertEqual(result['result'], expected)
                self.assertEqual(calls, {'start': 1, 'deallocate': 1})


BASELINE = ['bash-0:5.2.26-4.el10.x86_64', 'glibc-0:2.39-17.el10.x86_64']
OURS = ['podman-5:5.4.0-1.el10.x86_64', 'crun-0:1.20-1.el10.x86_64']


class Packages(Base):
    """R6-PACKAGES-001 + kai: the owned delta is exactly OUR transaction."""

    def receipt(self, history=None, after=None, last=41):
        history = history if history is not None else [{'id': 41, 'actions': [['install', 'bash-0:5.2.26-4.el10.x86_64']]},
                                                       {'id': 42, 'actions': [['install', n] for n in OURS]}]
        return runner.package_receipt(BASELINE, last, after if after is not None else BASELINE + OURS, history)

    def test_exactly_our_transaction_is_owned_and_undone_by_id(self):
        receipt = self.receipt()
        self.assertEqual(receipt, {'transaction_id': 42, 'delta': sorted(OURS),
                                   'cleanup_argv': ['dnf', 'history', 'undo', '--assumeyes', '42']})
        self.assertEqual(runner.package_undo_preview(receipt, [['remove', n] for n in OURS]), receipt['cleanup_argv'])
        self.assertEqual(runner.package_readback(BASELINE, list(BASELINE), True), 'clean')

    def test_any_intervening_or_mismatched_transaction_holds(self):
        ours = {'id': 42, 'actions': [['install', n] for n in OURS]}
        cases = {'intervening transaction': dict(history=[ours, {'id': 43, 'actions': [['install', 'jq-0:1.7-1.el10.x86_64']]}],
                                                 after=BASELINE + OURS + ['jq-0:1.7-1.el10.x86_64']),
                 'not the next id': dict(history=[dict(ours, id=44)]),
                 'wrong matching transaction': dict(history=[{'id': 42, 'actions': [['install', OURS[0]]]}]),
                 'partial transaction': dict(after=BASELINE + OURS[:1]),
                 'baseline package changed (upgrade)': dict(after=['bash-0:5.2.26-5.el10.x86_64', BASELINE[1]] + OURS,
                                                            history=[{'id': 42, 'actions': [['upgrade', 'bash-0:5.2.26-5.el10.x86_64']] +
                                                                      [['install', n] for n in OURS]}]),
                 'nothing new': dict(history=[], after=list(BASELINE))}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses(self.receipt, reason='package_ownership_hold', **change)

    def test_preview_allows_fresh_installs_only(self):
        self.assertEqual(runner.package_preview(BASELINE, [['install', n] for n in OURS]), sorted(OURS))
        for preview in ([['upgrade', 'bash-0:5.2.26-5.el10.x86_64']], [['install', BASELINE[0]]],
                        [['install', 'bash-0:5.3.0-1.el10.x86_64']], [['remove', BASELINE[1]]], []):
            with self.subTest(preview=preview):
                self.refuses(runner.package_preview, BASELINE, preview, reason='package_ownership_hold')

    def test_cleanup_failure_is_never_a_clean_host(self):
        receipt = self.receipt()
        self.refuses(runner.package_undo_preview, receipt, [['remove', OURS[0]], ['remove', BASELINE[0]]],
                     reason='package_ownership_hold')
        for after, user_absent in ((BASELINE + OURS[:1], True), (BASELINE, False), (BASELINE[:1], True)):
            with self.subTest(after=after, user_absent=user_absent):
                self.refuses(runner.package_readback, BASELINE, after, user_absent, reason='package_ownership_hold')


if __name__ == '__main__':
    unittest.main(verbosity=2)
