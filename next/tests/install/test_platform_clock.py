"""Source contract for the pure W4 clock mapper (design2 CLK01 + kai clock rule).

Literal samples are DATA stand-ins for the native module; they earn no native or
host credit. The native sample itself is tested in tests/install/native/.
"""
import ast
from pathlib import Path
import unittest

from cortex_core.install import owner
from cortex_core.install.contracts import measure_original_ref
from cortex_core.install.platforms import _clock

SUBJECT = Path(__file__).resolve().parents[2] / 'src/cortex_core/install/platforms/_clock.py'
NS = 1_000_000_000
BOUNDS = {'consent': 1_800_000_100, 'policy': 1_800_000_200, 'session': 1_800_000_050, 'host': 1_800_000_300}


def sample(**changes):
    value = {'platform': 'linux', 'm0': 500 * NS, 'm1': 500 * NS + 2_000, 'realtime_ns': 1_800_000_000 * NS,
             'boottime_ns': 500 * NS + 1_500, 'ntp_state': 0, 'ntp_status': 0x2001, 'maxerror_us': 4_013,
             'boot_id': '569d7749-895e-4ff1-b1bb-cd88d12d3393'}
    value.update(changes)
    return value


class ClockMapper(unittest.TestCase):
    def refuses(self, fn, *args):
        with self.assertRaises(owner.OwnerRefusal) as got:
            fn(*args)
        self.assertEqual(got.exception.detail, 'time_unavailable')

    def observe(self, s, anchor=None, bounds=BOUNDS):
        return _clock.observe(s, anchor or _clock.anchor(sample()), 'boot:launch', bounds, 'clock/receipt.json')

    def test_unsynced_or_unbounded_kernel_state_refuses_time_unavailable(self):
        for bad in (dict(ntp_status=0x2041), dict(ntp_state=5), dict(ntp_state=-1), dict(maxerror_us=500_001),
                    dict(maxerror_us=-1), dict(maxerror_us=True), dict(platform='win32'), dict(boot_id='')):
            with self.subTest(bad=bad):
                self.refuses(_clock.anchor, sample(**bad))
                self.refuses(self.observe, sample(**bad))
        missing = sample(); del missing['ntp_status']
        self.refuses(_clock.anchor, missing)
        self.refuses(_clock.anchor, dict(sample(), extra=1))
        self.observe(sample(maxerror_us=500_000))  # the cap itself is admitted (kai: <= 500 ms)

    def test_maxerror_maps_to_ceiling_uncertainty(self):
        for maxerror, span, expected in ((0, 0, 1), (4_013, 2_000, 2), (348_377, 2_167, 2), (500_000, 1_000_000, 2)):
            with self.subTest(maxerror=maxerror):
                s = sample(maxerror_us=maxerror, m1=500 * NS + span, boottime_ns=500 * NS + span)
                record = owner._decode(self.observe(s, _clock.anchor(s))[0], 'time_unavailable')
                self.assertEqual(record['uncertainty_seconds'], expected)

    def test_suspend_offset_or_wall_step_refuses_time_unavailable(self):
        anchor = _clock.anchor(sample())
        self.refuses(self.observe, sample(boottime_ns=500 * NS + 1_500 + 2_000_000), anchor)  # 2 ms suspend
        self.refuses(self.observe, sample(realtime_ns=1_800_000_003 * NS), anchor)  # 3 s wall step
        self.refuses(self.observe, sample(boot_id='another-boot'), anchor)
        self.refuses(_clock.observe, sample(), dict(anchor, extra=0), 'e', BOUNDS, 'clock/r.json')
        later = 7 * NS  # same offsets later in the epoch: admitted
        self.observe(sample(m0=500 * NS + later, m1=500 * NS + later + 2_000,
                            boottime_ns=500 * NS + later + 1_500, realtime_ns=1_800_000_007 * NS), anchor)

    def test_clock_bracket_span_over_limit_refuses_time_unavailable(self):
        self.refuses(_clock.anchor, sample(m1=500 * NS + 1_000_001))
        self.refuses(_clock.anchor, sample(m1=500 * NS - 1))  # m1 before m0
        _clock.anchor(sample(m1=500 * NS + 1_000_000))

    def test_clock_mapping_uses_earlier_anchor_and_rate_bound(self):
        # Hale's witness: UTC 100 read somewhere in mono [10, 13], expiry 105, u = 1 s.
        s = {'m0': 10 * NS, 'm1': 13 * NS, 'realtime_ns': 100 * NS}
        got = _clock._mono_deadline(s, NS, 105)
        self.assertEqual(got, 10 * NS + (4 * NS * 1_000_000) // 1_000_500)
        self.assertLess(got, 15 * NS)  # true UTC 105 can arrive at mono 15 at the latest-read case
        self.assertLess(_clock._mono_deadline(s, NS, 100), s['m1'])  # no headroom: already past "now", owner refuses
        self.assertEqual(_clock._mono_deadline(dict(s, m0=0), NS, 100), 0)  # never negative
        self.refuses(self.observe, dict(sample(), m0=10 * NS, m1=13 * NS))  # and the span cap refuses it anyway

    def test_mono_deadline_mapping_never_exceeds_utc_bound(self):
        for m0, span, maxerror, slack in ((0, 0, 0, 1), (10**15, 999_999, 500_000, 29), (7, 1, 1, 10**6)):
            s = sample(m0=m0, m1=m0 + span, boottime_ns=m0 + span, maxerror_us=maxerror)
            u = maxerror * 1000 + span + NS
            bound = s['realtime_ns'] // NS + slack
            deadline = _clock._mono_deadline(s, u, bound)
            # Even if the realtime read happened at m0 and UTC runs 500 ppm fast,
            # true UTC at `deadline` is still before the bound.
            latest_utc_at_deadline = s['realtime_ns'] + u + (deadline - m0) * 1_000_500 // 1_000_000
            with self.subTest(slack=slack):
                self.assertLessEqual(latest_utc_at_deadline, bound * NS)
                self.assertGreaterEqual(deadline, 0)

    def test_output_is_exactly_the_existing_owner_clock_and_receipt_shape(self):
        raw, ref, receipt_raw = self.observe(sample())
        record = owner._decode(raw, 'time_unavailable')
        owner._clock(record, 'boot:launch')
        self.assertEqual(set(record), owner._CLOCK)
        self.assertEqual(record['clock_receipt_ref'], ref)
        self.assertEqual(record['monotonic_ns'], sample()['m1'])  # later anchor for "now"
        measure_original_ref(ref, receipt_raw)
        receipt = owner._decode(receipt_raw, 'time_unavailable')
        self.assertEqual(receipt['quality'], 'bounded')
        self.assertEqual(set(receipt['monotonic_deadlines']), set(BOUNDS))
        with_private = self.observe(sample(), bounds=dict(BOUNDS, private=1_800_000_010))
        self.assertIn('private', owner._decode(with_private[2], 'x')['monotonic_deadlines'])
        self.refuses(self.observe, sample(), None, {'consent': 1})
        self.refuses(self.observe, sample(), None, dict(BOUNDS, other=1))

    def test_mapper_is_pure_arithmetic_without_io_or_clock_reads(self):
        tree = ast.parse(SUBJECT.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(n.name.split('.')[0] for n in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or '.')
        self.assertEqual(imported, {'cortex_core.release.codec', '.'})
        self.assertEqual(_clock.__all__, ())


if __name__ == '__main__':
    unittest.main()
