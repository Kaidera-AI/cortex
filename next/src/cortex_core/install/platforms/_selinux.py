"""V's own observation of the EFFECTIVE SELinux policy (design3 sec. 2.3, Vera accepted).

V reads what the kernel has loaded now: the sha256 of /sys/fs/selinux/policy (the
kernel serialises its current policydb, booleans included) and the status page
(version, sequence, enforcing, policyload, deny_unknown). It reads the status at
the start and again at the verdict; any change between them (policy load, boolean
commit, setenforce) refuses. The result is the receipt member `selinux`:
OBSERVATION ONLY. O's admitted value is a separate original; comparing the two is
the gate owner's proposed BL-G30 row, not done here. No module-artifact hash is
used. Peer namespaces are not observable by a zero-capability V (lab L-N1), so
they stay `required_binding_gap` in linux.py; nothing here fills them.
"""
import hashlib
import struct

from .. import owner

__all__ = ()
SELINUXFS = '/sys/fs/selinux'
_STATUS = struct.Struct('<5I')  # struct selinux_kernel_status, version >= 1
_FIELDS = ('version', 'sequence', 'enforcing', 'policyload', 'deny_unknown')
_POLICY_MAX = 64 * 1024 * 1024  # stock Rocky 10 targeted policy: 3427771 bytes
_CHUNK = 1024 * 1024


def _status(raw):
    """Decode one status-page read; refuse a torn (odd sequence) or unknown page."""
    owner._require(type(raw) is bytes and len(raw) >= _STATUS.size, 'owner_unavailable')
    page = dict(zip(_FIELDS, _STATUS.unpack_from(raw)))
    owner._require(page['version'] >= 1 and page['sequence'] % 2 == 0
                   and page['enforcing'] in (0, 1) and page['deny_unknown'] in (0, 1), 'owner_unavailable')
    return page


def _read_status(root):
    try:
        with open(root + '/status', 'rb') as f:
            return _status(f.read(_STATUS.size))
    except OSError:
        raise owner.OwnerRefusal('owner_unavailable') from None


def _policy_sha256(root):
    """One open = one kernel snapshot; -EBUSY (another reader) or any error refuses."""
    digest, size = hashlib.sha256(), 0
    try:
        with open(root + '/policy', 'rb') as f:
            while chunk := f.read(_CHUNK):
                size += len(chunk)
                owner._require(size <= _POLICY_MAX, 'owner_unavailable')
                digest.update(chunk)
    except OSError:
        raise owner.OwnerRefusal('owner_unavailable') from None
    owner._require(size > 0, 'owner_unavailable')
    return digest.hexdigest()


def start(*, root=SELINUXFS):
    """At request start: status s0, then the policy digest; s0 must still hold after the read."""
    s0 = _read_status(root)
    digest = _policy_sha256(root)
    owner._require(_read_status(root) == s0, 'custody_changed')
    return {'policy_sha256': digest, 's0': s0}


def verdict(observed, *, root=SELINUXFS):
    """At the verdict: status s1 must equal s0 exactly. Returns the receipt member `selinux`."""
    owner._require(type(observed) is dict and set(observed) == {'policy_sha256', 's0'}, 'owner_unavailable')
    s1 = _read_status(root)
    owner._require(s1 == observed['s0'], 'custody_changed')
    return {'policy_sha256': observed['policy_sha256'], 's0': dict(observed['s0']), 's1': s1}
