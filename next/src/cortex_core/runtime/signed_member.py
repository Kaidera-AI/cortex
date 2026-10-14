"""Consumer port for R01 original external signed-member authority (UNBOUND).

There is deliberately no verifier, root store, binding API or candidate reader.
The future authority owns aggregate/root/member authentication and byte custody.
Only this module can mint a result through its supported API. Its private registry
also rejects object.__new__ forgeries, copied metadata, dicts and duck types.
This is an in-process capability boundary, not a sandbox against trusted Python
rewriting module globals/closures. Tests replace the internal authority call only.
"""
from collections.abc import Callable
from typing import NoReturn, Protocol
from weakref import WeakKeyDictionary

from cortex_core.release.codec import sha256

MISSING_UNIT = 'R01 unit 3: signed Core/API aggregate and member admission'
MAX_MEMBER_BYTES = 262144


class Refusal(ValueError):
    """Closed outward failure; no candidate data or dependency exception text."""

    def __init__(self, reason: str, *, unit: str | None = None):
        self.reason = reason
        self.unit = unit
        super().__init__(reason if unit is None else reason + ': ' + unit)


class _AuthorityDelivery(Protocol):
    """R01-owned future delivery, not an admission result or caller binding API."""

    aggregate_id: str
    signing_root_id: str
    member_name: str
    member_sha256: str
    read_original: Callable[[int], bytes]


def _unbound(aggregate_id: str, member_name: str) -> NoReturn:
    # Must precede even input inspection: production has no authority to read.
    raise Refusal('required_binding_gap', unit=MISSING_UNIT)


_original_authority: Callable[[str, str], _AuthorityDelivery] = _unbound


def _port():
    # Neither a seal token nor a mint function escapes this closure.
    records = WeakKeyDictionary()

    class OriginalMember:
        """Opaque, immutable receipt; construction alone never confers provenance."""

        __slots__ = ('__weakref__',)

        def __new__(cls):
            raise TypeError('OriginalMember is created only by the signed-member port')

        @property
        def aggregate_id(self) -> str:
            return checked(self)[0]

        @property
        def signing_root_id(self) -> str:
            return checked(self)[1]

        @property
        def member_name(self) -> str:
            return checked(self)[2]

        @property
        def member_sha256(self) -> str:
            return checked(self)[3]

    def checked(member):
        if type(member) is not OriginalMember or member not in records:
            raise Refusal('unsealed_member')
        return records[member]

    def request_member(aggregate_id: str, member_name: str) -> OriginalMember:
        """Ask R01 for one original member. Production always raises its unit gap."""
        try:
            delivery = _original_authority(aggregate_id, member_name)
            identities = (delivery.aggregate_id, delivery.signing_root_id,
                          delivery.member_name, delivery.member_sha256)
            if (any(type(value) is not str or not value or len(value) > 512 for value in identities)
                    or type(aggregate_id) is not str or type(member_name) is not str
                    or identities[0] != aggregate_id or identities[2] != member_name):
                raise Refusal('member_identity_mismatch')
            raw = delivery.read_original(MAX_MEMBER_BYTES)
            if type(raw) is not bytes or len(raw) > MAX_MEMBER_BYTES:
                raise Refusal('raw_limit')
            if sha256(raw) != identities[3]:
                raise Refusal('member_digest_mismatch')
        except Refusal:
            raise
        except Exception:
            raise Refusal('member_authority_unavailable') from None
        member = object.__new__(OriginalMember)
        records[member] = (*identities, raw)
        return member

    def read_member(member: OriginalMember, *, aggregate_id: str, member_name: str) -> bytes:
        """Sealed reader: return the exact immutable bytes bound to this request."""
        record = checked(member)
        if record[0] != aggregate_id or record[2] != member_name:
            raise Refusal('member_identity_mismatch')
        return record[4]

    return OriginalMember, request_member, read_member


OriginalMember, request_member, read_member = _port()
del _port
