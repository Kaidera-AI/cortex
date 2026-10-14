"""One unsigned original-byte assembler. Production owner delivery is UNBOUND.

No signer, verifier duplicate, record/capability class, binder or effects. Private
fixture composition is source data, not native/installed acceptance. The future
contracts export/member bridge is owned by the separately dispatched U3-D/F.
"""
from cortex_core.runtime.signed_member import Refusal
from .codec import sha256
from .trust import _unbound,_owner_policy,_current,_require
from .aggregate import _validate_manifest,_measure_members,_check_pairs


def assemble_aggregate(expected_raw, root, builds):
    """Fixed production authority first, before any argument or physical read."""
    _unbound()


def _assemble_original(expected_raw, reader, builds, context):
    """Owner-internal generic algorithm over exact unsigned originals only.

    Expected rows/policies/maps must be independently admitted by the eventual
    owner composition; neither artifact flags nor this private Python call grant
    provenance. Returns bytes, never a member/native/effect receipt.
    """
    try:
        policy=_owner_policy(context)
        _require(context.placement in ('installer','owner'),'aggregate_scope_mismatch')
        value,limits=_validate_manifest(expected_raw,policy)
        _current(policy,value,context)
        _require(sha256(expected_raw)==policy['expected_aggregate_sha256'],'aggregate_identity_mismatch')
        bodies=_measure_members(value,reader,limits,overhead_bytes=len(expected_raw))
        _check_pairs(value,bodies,builds,limits)
        reader.aggregate_finish()
        return expected_raw
    except Refusal:raise
    except Exception:raise Refusal('member_authority_unavailable') from None
    finally:reader.close()
