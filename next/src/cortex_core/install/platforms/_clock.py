"""Pure clock mapper for the fixed native observers: arithmetic only, no IO.

A native sample is DATA from V's admitted native module, never from C. The only
admitted UTC source is the host's kernel-disciplined clock (kai ruling 15 Oct):
synchronised, maxerror <= 500 ms. The kernel grows maxerror by 500 us for every
second without a daemon update, so that cap also bounds the last sync to <= 1000 s.
Every sample is bracketed by two Python-domain monotonic reads (m0, m1). Deadlines
map from the EARLIER anchor with the full error and a 500 ppm rate bound; "now"
uses the LATER anchor. owner._clock/_window/_admitted_deadline stay the judges.
"""
from cortex_core.release.codec import canonical_bytes, sha256

from .. import owner

__all__ = ()
_SAMPLE = frozenset(('platform m0 m1 realtime_ns boottime_ns ntp_state ntp_status maxerror_us boot_id').split())
_ANCHOR = frozenset(('boot_id suspend_offset_ns wall_offset_ns').split())
_INT_MAX = 2**63 - 1
_UTC_MAX = 253402300799
_MAX_ERROR_US = 500_000
_MAX_SPAN_NS = 1_000_000
_SUSPEND_TOLERANCE_NS = 1_000_000
_STA_UNSYNC = 0x0040
_TIME_ERROR = 5
_RATE = (1_000_000, 1_000_500)  # 500 ppm: kernel frequency tolerance


def _int(value, minimum=0, maximum=_INT_MAX):
    owner._require(type(value) is int and minimum <= value <= maximum, 'time_unavailable')
    return value


def _quality(sample):
    """Refuse anything but a coherent, synchronised, bounded kernel sample."""
    owner._require(type(sample) is dict and set(sample) == _SAMPLE, 'time_unavailable')
    owner._require(sample['platform'] in ('darwin', 'linux'), 'time_unavailable')
    owner._require(type(sample['boot_id']) is str and 0 < len(sample['boot_id'].encode('utf-8')) <= 128, 'time_unavailable')
    for key in ('m0', 'm1', 'realtime_ns', 'boottime_ns', 'ntp_status'):
        _int(sample[key])
    _int(sample['ntp_state'], 0, _TIME_ERROR - 1)  # TIME_OK..TIME_WAIT; TIME_ERROR refuses
    owner._require(not sample['ntp_status'] & _STA_UNSYNC, 'time_unavailable')
    _int(sample['maxerror_us'], 0, _MAX_ERROR_US)
    owner._require(0 <= sample['m1'] - sample['m0'] <= _MAX_SPAN_NS, 'time_unavailable')
    return sample


def anchor(sample):
    """Epoch-start offsets; a later suspend, wall step or reboot invalidates."""
    s = _quality(sample)
    return {'boot_id': s['boot_id'], 'suspend_offset_ns': s['boottime_ns'] - s['m1'],
            'wall_offset_ns': s['realtime_ns'] - s['boottime_ns']}


def _uncertainty_ns(s):
    # kernel bound + capture span + flooring UTC to whole seconds
    return s['maxerror_us'] * 1000 + (s['m1'] - s['m0']) + 1_000_000_000


def _mono_deadline(s, u_ns, utc_bound):
    _int(utc_bound, 0, _UTC_MAX)
    headroom = utc_bound * 1_000_000_000 - s['realtime_ns'] - u_ns
    # Earlier anchor and slowest admitted clock rate; floor; never negative.
    return min(_INT_MAX, max(0, s['m0'] + headroom * _RATE[0] // _RATE[1]))


def observe(sample, epoch_anchor, owner_epoch, bounds, receipt_path):
    """Return (clock_record_bytes, receipt_ref, receipt_bytes) in existing shapes.

    bounds: the current original UTC expiries named exactly as owner._window
    expects (consent, policy, session, host and optional private).
    """
    s = _quality(sample)
    owner._require(type(epoch_anchor) is dict and set(epoch_anchor) == _ANCHOR, 'time_unavailable')
    owner._require(s['boot_id'] == epoch_anchor['boot_id'], 'time_unavailable')
    u_ns = _uncertainty_ns(s)
    suspend = s['boottime_ns'] - s['m1']
    owner._require(abs(suspend - epoch_anchor['suspend_offset_ns']) <= _SUSPEND_TOLERANCE_NS, 'time_unavailable')
    wall = s['realtime_ns'] - s['boottime_ns']
    owner._require(abs(wall - epoch_anchor['wall_offset_ns']) <= u_ns + _SUSPEND_TOLERANCE_NS, 'time_unavailable')
    owner._require(type(owner_epoch) is str and 0 < len(owner_epoch.encode('utf-8')) <= 128, 'time_unavailable')
    owner._require(type(bounds) is dict and {'consent', 'policy', 'session', 'host'} <= set(bounds)
                   <= {'consent', 'policy', 'session', 'host', 'private'}, 'time_unavailable')
    receipt = {'owner_epoch': owner_epoch, 'quality': 'bounded',
               'monotonic_deadlines': {k: _mono_deadline(s, u_ns, v) for k, v in bounds.items()}}
    receipt_raw = canonical_bytes(receipt)
    ref = {'path': receipt_path, 'bytes': len(receipt_raw), 'sha256': sha256(receipt_raw)}
    record = {'owner_epoch': owner_epoch, 'now_utc': _int(s['realtime_ns'] // 1_000_000_000, 0, _UTC_MAX),
              'uncertainty_seconds': -(-u_ns // 1_000_000_000), 'monotonic_ns': s['m1'],
              'clock_receipt_ref': ref}
    return canonical_bytes(record), ref, receipt_raw
