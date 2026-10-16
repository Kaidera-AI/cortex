"""X12 installer enrollment: one first identity, one scoped roundtrip, one D3 reader.

Transport (kai Q1, 16 Oct): the enrollment request is an install-owner ORIGINAL
named by the consented effect's expected_after_contract_ref. It runs only at P2a's
consumed dispatch, which stays UNBOUND here; its canonical receipt is the record a
P1 receipt cites as readiness.checks_ref. No route, invitation or second identity.
Cadence (kai Q2): the human roundtrip happens once, at enrollment. Every health
call re-reads 006 and re-resolves the roundtrip scope through the request
session, so an appliance restart never needs the human again.
Values stay with their custodians: the request carries opaque UUIDs, a credential
digest, an idempotency key and a resolver handle, never a secret.
"""
import re
from uuid import UUID

from cortex_core.install.contracts import InstallRefusal, measure_original_ref, parse_install_message
from cortex_core.release.codec import canonical_bytes, decode_json, sha256
from cortex_core.release.contracts import freeze, thaw
from .signed_member import Refusal

REQUEST_SCHEMA = 'cortex.enrollment-request.v1'
RECEIPT_SCHEMA = 'cortex.enrollment-receipt.v1'
EFFECT_ID = 'cortex.enrollment.first-identity'
MISSING_UNIT = 'P2a consumed dispatch for the enrollment effect family'
ADMISSION_UNIT = 'X12-c owner delivery of the accepted P1 enrollment original'
FIRST_IDENTITY_SQL = 'SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)'
READINESS_SQL = 'SELECT auth.enrollment_readiness(%s)::text'
SCOPE_SQL = ("SELECT installation_id::text,tenant_id::text,project_id::text,principal_id::text "
             "FROM auth.resolve_scope(%s,%s,%s,'read')")
_LIMIT = 16384
_IDS = ('installation_id', 'tenant_id', 'project_id', 'principal_id', 'credential_id')
_REQUEST = frozenset(_IDS + ('schema', 'credential_digest', 'idempotency_key', 'consent_ref', 'roundtrip_handle'))
_RECEIPT = frozenset(_IDS + ('schema', 'credential_digest', 'request_sha256', 'consent_ref', 'roundtrip'))
_ROUNDTRIP = frozenset(('route', 'status', 'principal_id', 'project_id', 'response_sha256'))
_ANSWER = frozenset(('principal_id', 'credential_id', 'project_id', 'replayed'))
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_KEY = re.compile(r'[\x21-\x7e]{1,256}\Z')


def _require(value, reason):
    if not value:
        raise Refusal(reason)


def _unbound(_effect_context):
    raise Refusal('required_binding_gap', unit=MISSING_UNIT)


# P2a's consumed dispatch is the only authority; the fixed owner binds it later.
_consumed_dispatch = _unbound


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _ref(value):
    return (type(value) is dict and set(value) == {'path', 'bytes', 'sha256'} and type(value['path']) is str
            and 0 < len(value['path']) <= 4096 and type(value['bytes']) is int and value['bytes'] >= 0
            and type(value['sha256']) is str and _HEX.fullmatch(value['sha256']) is not None)


def _decode(raw, reason):
    try:
        value = decode_json(raw, _LIMIT)
    except (ValueError, TypeError, RecursionError):
        raise Refusal(reason) from None
    _require(type(value) is dict, reason)
    return value


def parse_request(raw):
    """Closed canonical request; identity atoms only, no value or default."""
    reason = 'enrollment_request_invalid'
    doc = _decode(raw, reason)
    _require(set(doc) == _REQUEST and doc['schema'] == REQUEST_SCHEMA, reason)
    _require(all(_uuid(doc[k]) for k in _IDS + ('roundtrip_handle',)), reason)
    _require(type(doc['credential_digest']) is str and _HEX.fullmatch(doc['credential_digest']) is not None, reason)
    _require(type(doc['idempotency_key']) is str and _KEY.fullmatch(doc['idempotency_key']) is not None, reason)
    _require(_ref(doc['consent_ref']), reason)
    return freeze(doc)


def parse_receipt(raw):
    """The one parser for the receipt, used by enroll's output and the D3 reader."""
    reason = 'enrollment_receipt_invalid'
    doc = _decode(raw, reason)
    _require(set(doc) == _RECEIPT and doc['schema'] == RECEIPT_SCHEMA, reason)
    _require(all(_uuid(doc[k]) for k in _IDS), reason)
    _require(all(type(doc[k]) is str and _HEX.fullmatch(doc[k]) is not None
                 for k in ('credential_digest', 'request_sha256')), reason)
    _require(_ref(doc['consent_ref']), reason)
    trip = doc['roundtrip']
    _require(type(trip) is dict and set(trip) == _ROUNDTRIP and trip['route'] == 'C11-D1'
             and type(trip['status']) is int and trip['status'] == 200
             and (trip['principal_id'], trip['project_id']) == (doc['principal_id'], doc['project_id'])
             and type(trip['response_sha256']) is str and _HEX.fullmatch(trip['response_sha256']) is not None, reason)
    return freeze(doc)


def _admitted(effect_context, raw):
    _consumed_dispatch(effect_context)
    reason = 'enrollment_scope_denied'
    try:
        context = thaw(effect_context)['context']
        effect, identity = context['effect'], context['identity']
        _require(effect['effect_id'] == EFFECT_ID and effect['product'] == identity['product'] == 'cortex', reason)
        measure_original_ref(effect['expected_after_contract_ref'], raw)
    except (InstallRefusal, KeyError, TypeError):
        raise Refusal(reason) from None
    request = parse_request(raw)
    _require(identity['installation_id'] == request['installation_id'], reason)
    return request


def enroll(effect_context, request_raw, *, first_identity, roundtrip):
    """Run inside P2a's consumed dispatch: ONE first identity, then ONE scoped read.

    A lost reply is reconciled by calling again with the SAME original: 005 replays
    the one-use receipt, so a retry never creates a second identity. Returns the
    canonical receipt bytes (identical on every successful retry).
    """
    request = _admitted(effect_context, request_raw)
    try:
        answer = first_identity(request)
    except Refusal:
        raise
    except Exception:
        raise Refusal('enrollment_unavailable') from None
    _require(type(answer) is dict and set(answer) == _ANSWER and type(answer['replayed']) is bool
             and (answer['principal_id'], answer['credential_id'], answer['project_id'])
             == (request['principal_id'], request['credential_id'], request['project_id']), 'enrollment_unavailable')
    try:
        trip = roundtrip(request)
    except Exception:
        raise Refusal('enrollment_roundtrip_failed') from None
    _require(type(trip) is dict and set(trip) == _ROUNDTRIP and trip['route'] == 'C11-D1'
             and type(trip['status']) is int and trip['status'] == 200
             and (trip['principal_id'], trip['project_id']) == (request['principal_id'], request['project_id'])
             and type(trip['response_sha256']) is str and _HEX.fullmatch(trip['response_sha256']) is not None,
             'enrollment_roundtrip_mismatch')
    receipt = {k: request[k] for k in _IDS + ('credential_digest',)}
    receipt.update(schema=RECEIPT_SCHEMA, request_sha256=sha256(request_raw),
                   consent_ref=thaw(request['consent_ref']), roundtrip=dict(trip))
    raw = canonical_bytes(receipt)
    parse_receipt(raw)
    return raw


def first_identity_sql(cursor, request):
    """The installer_enrollment login's single call; 005 decides everything else."""
    try:
        cursor.execute(FIRST_IDENTITY_SQL, tuple(request[k] for k in _IDS + ('credential_digest', 'idempotency_key')))
        row = cursor.fetchone()
    except Exception as error:
        try:
            denied = error.sqlstate == '42501'  # psycopg InsufficientPrivilege
        except AttributeError:
            denied = False
        raise Refusal('enrollment_forbidden' if denied else 'enrollment_unavailable') from None
    _require(type(row) is tuple and len(row) == 1 and type(row[0]) is dict, 'enrollment_unavailable')
    return dict(row[0])


async def readiness_sql(connection, installation_id):
    """006 through the request session (EXECUTE granted only to that role)."""
    async with connection.cursor() as cursor:
        await cursor.execute(READINESS_SQL, (str(installation_id),))
        row = await cursor.fetchone()
    return row[0] if type(row) is tuple and len(row) == 1 else None


async def scope_sql(connection, digest, installation_id, project_id):
    """The existing request-role scope resolver; read-only, no new SQL."""
    async with connection.cursor() as cursor:
        await cursor.execute(SCOPE_SQL, (digest, str(installation_id), str(project_id)))
        rows = await cursor.fetchall()
    names = ('installation_id', 'tenant_id', 'project_id', 'principal_id')
    return [dict(zip(names, row)) for row in rows]


def _no_accepted_enrollment():
    raise Refusal('required_binding_gap', unit=ADMISSION_UNIT)


# The fixed install owner delivers the ACCEPTED original from its protected P1
# history (X12-c binds it); never a caller parameter. It returns None only when
# the owner attests that no enrollment was recorded, else a mapping with the P1
# receipt bytes, the stored enrollment receipt bytes and the consumed request ref.
_accepted_enrollment = _no_accepted_enrollment


def _admitted_receipt():
    """The receipt only if it IS the accepted original (X12-001); None = no enrollment.

    The P1 receipt (sole P1 parser) must be ready for this enrollment with a
    checks_ref; the stored receipt's exact bytes must measure to that checks_ref;
    and the receipt must name the recorded request digest and the P1 installation.
    """
    delivered = _accepted_enrollment()
    if delivered is None:
        return None
    reason = 'enrollment_receipt_invalid'
    try:
        _require(type(delivered) is dict and set(delivered) == {'p1_receipt', 'enrollment_receipt', 'request_ref'}, reason)
        p1 = thaw(parse_install_message(delivered['p1_receipt'], expected_kind='receipt').document)
        _require(p1['identity']['product'] == 'cortex' and p1['readiness']['state'] == 'ready', reason)
        raw = delivered['enrollment_receipt']
        measure_original_ref(p1['readiness']['checks_ref'], raw)
        receipt = thaw(parse_receipt(raw))
        request_ref = delivered['request_ref']
        _require(_ref(request_ref) and receipt['request_sha256'] == request_ref['sha256']
                 and receipt['installation_id'] == p1['identity']['installation_id'], reason)
    except (InstallRefusal, KeyError, TypeError):
        raise Refusal(reason) from None
    return receipt


def enrollment_state_reader(readiness, resolve_scope):
    """D3: the existing EnrollmentHealth enum port, `ready` only on BOTH truths.

    (a) 006 says ready for THIS installation and (b) the ACCEPTED X12 receipt
    (owner-delivered, measured against the P1 checks_ref) still resolves to
    exactly its (installation, tenant, default project, principal) through
    auth.resolve_scope. Owner-attested no enrollment: 006's enrollment_required
    passes through, anything else is unavailable. Unbound owner delivery or any
    admission failure: always unavailable. Errors are unavailable; the caller's
    EnrollmentHealth timeout bounds both reads (X5's remaining budget, no new SLA).
    """
    try:
        receipt = _admitted_receipt()
        broken = False
    except Refusal:
        receipt, broken = None, True

    async def enrollment_state(installation_id):
        try:
            if broken or not isinstance(installation_id, UUID):
                return 'unavailable'
            state = await readiness(installation_id)
            if receipt is None:
                return 'enrollment_required' if state == 'enrollment_required' else 'unavailable'
            if state != 'ready' or receipt['installation_id'] != str(installation_id):
                return 'unavailable'
            rows = await resolve_scope(receipt['credential_digest'], installation_id, receipt['project_id'])
            expected = {k: receipt[k] for k in ('installation_id', 'tenant_id', 'project_id', 'principal_id')}
            return 'ready' if type(rows) is list and rows == [expected] else 'unavailable'
        except Exception:
            return 'unavailable'
    return enrollment_state
