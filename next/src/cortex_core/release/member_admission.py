"""Sole read-only R01 backend to the existing RC2 original-member port.

Fixed owner composition is UNBOUND before arguments/imports/physical reads.
Private context/transport callables are trusted installed-owner dependencies,
never client parameters or an authority binder. Source test composition proves
neither protected installation nor current native/image/effect authority.
Images retain their separate future stream gate; this snapshot stays <=262144.
"""
import re
import time
from types import SimpleNamespace

from cortex_core.runtime.signed_member import Refusal, MISSING_UNIT, MAX_MEMBER_BYTES
from .codec import sha256

_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NAME = re.compile(r'members/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)*\Z')


def _owner_originals():
    # Fixed missing owner delivery, not a second root/provider or public setter.
    # Future installed composition supplies the SAME trust current context and
    # admitted original transport; dependency provenance is a U3-E/native gate.
    raise Refusal('required_binding_gap', unit=MISSING_UNIT)


def request_original_member(aggregate_id, member_name):
    """Private production backend; no caller path/root/policy/config binding."""
    try:
        current, originals = _owner_originals()
        return _request_original(aggregate_id, member_name, current, originals)
    except Refusal:
        raise
    except Exception:
        raise Refusal('member_authority_unavailable') from None


def _request_original(aggregate_id, member_name, current, originals):
    """One generic algorithm for admitted owner originals; read-only DATA.

    Reuses B authentication and C complete original custody/paired closure.
    Both initial admission and bounded delivery use this body. The deadline is
    shared for their full work; no stream/pass/chunk timer reset. All descriptors
    close before an immutable original snapshot leaves the backend. RC2's seal
    remains the only OriginalMember constructor and never grants later effects.
    """
    if (type(aggregate_id) is not str or type(member_name) is not str
            or not _ID.fullmatch(aggregate_id) or len(member_name)>512
            or not (member_name=='runtime.lock.json' or _NAME.fullmatch(member_name))):
        raise Refusal('member_identity_mismatch')

    # Lazy package imports: production missing authority reads no package files.
    from . import aggregate, trust
    from .custody import RootReader
    require = trust._require
    deadline = time.monotonic()+aggregate.PROVISIONAL_LIMITS['read_seconds']

    def budget():
        require(time.monotonic()<=deadline, 'raw_limit')

    def admit(limit):
        nonlocal deadline
        reader = None
        try:
            require(type(limit) is int and 0<limit<=MAX_MEMBER_BYTES, 'raw_limit')
            budget()
            context = current()
            policy = trust._owner_policy(context)
            limits = aggregate._limits(policy)
            # Tightening cannot restart or extend this request/delivery budget.
            deadline = min(deadline, started+limits['read_seconds'])
            budget()
            raw, signature, reader, builds = originals(aggregate_id)
            require(type(reader) is RootReader, 'required_binding_gap')
            require(type(raw) is bytes and len(raw)<=limits['manifest_bytes'], 'raw_limit')
            require('sha256:'+sha256(raw)==aggregate_id, 'aggregate_identity_mismatch')
            authenticated = trust._verify_original(raw, signature, context)
            value, limits = aggregate._validate_manifest(raw, policy)
            rows = [row for row in value['members'] if row['path']==member_name]
            require(len(rows)==1, 'member_not_in_aggregate')
            row = rows[0]
            require(row['kind']!='image' and row['bytes']<=limit, 'raw_limit')
            # Reuse C's per-read/chunk timer with only the remaining budget.
            # A later delivery cannot buy a fresh 120-second physical pass.
            read_limits = dict(limits, read_seconds=min(limits['read_seconds'],
                                                        deadline-time.monotonic()))
            require(read_limits['read_seconds']>0, 'raw_limit')
            bodies = aggregate._measure_members(value, reader, read_limits,
                                               overhead_bytes=len(raw)+len(signature))
            budget()
            aggregate._check_pairs(value, bodies, builds, limits)

            def revalidate():
                budget()
                fresh = current()
                # The sole verifier validates required current owner state,
                # root/policy pins, scope and time; no duplicate policy parser.
                trust._verify_original(raw, signature, fresh)
                # Current lease/root/policy cannot change during one delivery,
                # even if a replacement policy could admit the same bytes.
                require(fresh.generation==context.generation and fresh.fence==context.fence
                        and fresh.policy_sha256==context.policy_sha256, 'aggregate_replay')
                budget()

            revalidate()
            reader.aggregate_finish()
            revalidate()
            # Current-owner reload can take time: retain the SAME held original
            # transaction and check all named/held identities immediately after.
            reader._aggregate_check()
            # C measured the whole original length/digest against this signed
            # row; its EOF recheck holds every original. Bytes are immutable.
            original = bodies[member_name]
            return original, authenticated
        except Refusal:
            raise
        except Exception:
            raise Refusal('member_authority_unavailable') from None
        finally:
            if reader is not None:
                try: reader.close()
                except Exception: raise Refusal('member_authority_unavailable') from None

    started = time.monotonic()
    original, authenticated = admit(MAX_MEMBER_BYTES)
    root_id = 'sha256:'+authenticated.signing_root_sha256
    member_digest = sha256(original)

    def read_original(limit):
        raw, fresh = admit(limit)
        require('sha256:'+fresh.signing_root_sha256==root_id, 'aggregate_root_mismatch')
        return raw

    # Internal data adapter to the EXISTING protocol, never a second seal/class.
    return SimpleNamespace(aggregate_id=aggregate_id, signing_root_id=root_id,
                           member_name=member_name, member_sha256=member_digest,
                           read_original=read_original)
