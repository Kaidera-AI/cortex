"""Sole R01 original-manifest signature verification; installer/owner only.

No signing, test-mode/root binder, transport, image/member admission or effects.
Production context is UNBOUND until independent owner/lock/trust delivery. The
private generic algorithm can be exercised with unrelated in-memory test roots
only from test support outside installed source. Python internals are a trusted
code boundary, not Vera's later OS/process isolation proof.
"""
import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass,field
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import re

from cortex_core.runtime.signed_member import Refusal
from .codec import WireValidator,canonical_bytes,decode_json,sha256
from .contracts import freeze,thaw
from .ed25519 import verify_ed25519

MISSING_UNIT='R01 unit 3: signed Core/API aggregate and member admission'
PRODUCTION_KEY_ID='7BBB4B7E0CE8852E'
MAX_MANIFEST=1048576
MAX_SIGNATURE=4096
MAX_POLICY=8388608
MAX_ROOT=4096
_PURPOSES={'cortex/core-api/test','cortex/core-api/production'}
_HEX=re.compile(r'[0-9a-f]{64}\Z')
_KEY_ID=re.compile(r'[0-9A-F]{16}\Z')
# Fixed, inherited contract subjects only; candidate paths/URLs are never read.
_SCHEMA_HOME=Path(__file__).resolve().parents[3]/'contracts/release'
_MANIFEST_VALIDATOR=WireValidator(json.loads((_SCHEMA_HOME/'aggregate.schema.json').read_bytes()))
_SHAPE_VALIDATOR=_MANIFEST_VALIDATOR
_SHAPE_TYPE_CHECKER=_MANIFEST_VALIDATOR.TYPE_CHECKER
_SHAPE_FORMAT_CHECKER=_MANIFEST_VALIDATOR.format_checker
_SHAPE_RESOLVER=_MANIFEST_VALIDATOR._resolver
_POLICY_SCHEMA=json.loads((_SCHEMA_HOME/'aggregate-policy.schema.json').read_bytes())
_POLICY_VALIDATOR=WireValidator(_POLICY_SCHEMA)
_BASELINE_VALIDATOR=WireValidator(_POLICY_SCHEMA['properties']['installed_baseline'])


@dataclass(frozen=True)
class _OwnerContext:
    """Independent owner delivery; never a candidate/config/argv parameter.

    A Python record is not provenance against trusted code rewriting internals.
    The production provider, original custody and installed owner proof are held.
    """
    policy: Mapping
    policy_raw: bytes
    policy_sha256: str
    public_root: bytes
    now: int
    generation: int
    fence: int
    placement: str
    current_state: Mapping
    # Optional private owner DATA. Legacy point observations retain their exact
    # semantics; the installed B1 composition supplies its full admitted bounds.
    clock_interval: tuple | None = None


@dataclass(frozen=True)
class ManifestSignature:
    """Immutable byte-authentication DATA, not a member/effect/native capability."""
    manifest_raw: bytes
    manifest_sha256: str
    signature_sha256: str
    signing_root_sha256: str
    signing_key_id: str
    manifest: Mapping
    native_qualified: bool=field(default=False,init=False)
    effect_authority: bool=field(default=False,init=False)
    members_admitted: bool=field(default=False,init=False)


def _require(value,reason):
    if not value:raise Refusal(reason)


def _unbound():
    raise Refusal('required_binding_gap',unit=MISSING_UNIT)


_production_context=_unbound


def _owner_policy(context):
    if (type(context) is not _OwnerContext or not isinstance(context.policy,Mapping)
            or type(context.policy_raw) is not bytes or len(context.policy_raw)>MAX_POLICY
            or type(context.policy_sha256) is not str or not _HEX.fullmatch(context.policy_sha256)):
        raise Refusal('required_binding_gap',unit=MISSING_UNIT)
    # Pin the exact independent owner document; no artifact supplies/repares it.
    _require(sha256(context.policy_raw)==context.policy_sha256,'required_binding_gap')
    try:
        policy=thaw(context.policy)
        _require(canonical_bytes(policy)==context.policy_raw,'required_binding_gap')
    except (ValueError,TypeError,RecursionError,OverflowError):
        raise Refusal('required_binding_gap',unit=MISSING_UNIT) from None
    _current_state(context)
    return policy


def _current_state(context):
    """Required current owner facts; baseline attestation is not a new lease."""
    state=getattr(context,'current_state',None)
    _require(isinstance(state,Mapping) and set(state)=={'installed_baseline','policy_generation','fence'},'required_binding_gap')
    _require(all(type(state[key]) is int and 1<=state[key]<=2**63-1
                 for key in ('policy_generation','fence')),'required_binding_gap')
    baseline=state['installed_baseline']
    _require(next(_BASELINE_VALIDATOR.iter_errors(thaw(baseline)),None) is None,'required_binding_gap')
    try:canonical_bytes(thaw(baseline))
    except (ValueError,TypeError,RecursionError,OverflowError):
        raise Refusal('required_binding_gap',unit=MISSING_UNIT) from None
    return state


def verify_manifest(raw: bytes, signature: bytes) -> ManifestSignature:
    """Production entry: fixed owner context FIRST, never a public root binder."""
    try:
        context=_production_context()
        policy=_owner_policy(context)
        _require(policy.get('root',{}).get('key_id')==PRODUCTION_KEY_ID,'aggregate_root_mismatch')
        if next(_POLICY_VALIDATOR.iter_errors(policy),None) is not None:
            raise Refusal('required_binding_gap',unit=MISSING_UNIT)
        return _verify_original(raw,signature,context)
    except Refusal:raise
    except Exception:
        raise Refusal('member_authority_unavailable') from None


def _base64(line,size,reason):
    try:
        decoded=base64.b64decode(line,validate=True)
    except (ValueError,binascii.Error):
        raise Refusal(reason) from None
    _require(len(decoded)==size and base64.b64encode(decoded)==line,reason)
    return decoded


def _root(context,policy):
    root=policy['root'];material=context.public_root
    _require(type(material) is bytes and 0<len(material)<=MAX_ROOT,'aggregate_root_mismatch')
    _require(type(root['public_key_bytes']) is int and root['public_key_bytes']==len(material),'aggregate_root_mismatch')
    _require(type(root['key_material_sha256']) is str and _HEX.fullmatch(root['key_material_sha256'])
             and sha256(material)==root['key_material_sha256'],'aggregate_root_mismatch')
    _require(type(root['key_id']) is str and _KEY_ID.fullmatch(root['key_id']),'aggregate_root_mismatch')
    _require(b'\r' not in material and b'\0' not in material,'aggregate_root_mismatch')
    lines=material.split(b'\n')
    _require(len(lines)==3 and lines[-1]==b'' and lines[0].startswith(b'untrusted comment: '),'aggregate_root_mismatch')
    parsed=_base64(lines[1],42,'aggregate_root_mismatch')
    _require(parsed[:2]==b'Ed' and parsed[2:10][::-1].hex().upper()==root['key_id'],'aggregate_root_mismatch')
    return parsed[2:10],parsed[10:]


def _envelope(signature,key_id):
    _require(b'\r' not in signature and b'\0' not in signature,'aggregate_signature_invalid')
    lines=signature.split(b'\n')
    _require(len(lines)==5 and lines[-1]==b'' and lines[0].startswith(b'untrusted comment: ')
             and lines[2].startswith(b'trusted comment: '),'aggregate_signature_invalid')
    try:signature.decode('utf-8')
    except UnicodeError:raise Refusal('aggregate_signature_invalid') from None
    packet=_base64(lines[1],74,'aggregate_signature_invalid')
    _require(packet[2:10]==key_id,'aggregate_root_mismatch')
    _require(packet[:2]==b'ED','aggregate_algorithm_unsupported')
    trusted=lines[2][len(b'trusted comment: '):]
    _require(len(trusted)<=512,'raw_limit')
    global_signature=_base64(lines[3],64,'aggregate_signature_invalid')
    return packet[10:],trusted,global_signature


def _depth(value,maximum,level=0):
    _require(level<=maximum,'raw_limit')
    if isinstance(value,dict):
        for key,item in value.items():_depth(key,maximum,level+1);_depth(item,maximum,level+1)
    elif isinstance(value,list):
        for item in value:_depth(item,maximum,level+1)


def _comment(raw,value):
    return ('cortex.release-aggregate.v1|purpose='+value['purpose']+'|release='+value['release']['release_id']+
            '|sequence='+str(value['sequence'])+'|sha256='+sha256(raw)+'|source='+value['source']['commit']).encode('utf-8')


def _current(policy,value,context):
    root=policy['root']
    _require(policy['purpose'] in _PURPOSES and value['purpose']==policy['purpose']
             and policy['purpose'] in root['allowed_purposes'] and root['key_use_permission']=='cortex-core-api-release',
             'aggregate_scope_mismatch')
    validity=policy['validity'];baseline=policy['installed_baseline'];owner=policy['owner_binding'];now=context.now
    state=_current_state(context)
    _require(canonical_bytes(thaw(baseline))==canonical_bytes(thaw(state['installed_baseline'])),'aggregate_replay')
    _require(type(now) is int and type(context.generation) is int and type(context.fence) is int
             and context.generation==owner['policy_generation']==state['policy_generation']
             and context.fence==state['fence'],'aggregate_replay')
    interval=getattr(context,'clock_interval',None)
    interval=(now,now) if interval is None else interval
    _require(type(interval) is tuple and len(interval)==2
             and all(type(n) is int and 0<=n<=253402300799 for n in interval)
             and interval[0]<=now<=interval[1],'aggregate_replay')
    lower,upper=interval
    _require(validity['not_before']<=lower<=upper<=validity['expires_at']
             and value['not_before']<=lower<=upper<=value['expires_at']
             and value['not_before']<=value['expires_at'] and validity['not_before']<=validity['expires_at']
             and upper-value['not_before']<=validity['maximum_release_age_seconds'],'aggregate_replay')
    _require(value['sequence']>=policy['sequence_floor'],'aggregate_replay')
    _require(value['release']==policy['release'] and value['source']==policy['source'],'aggregate_identity_mismatch')


def _parse_manifest_shape(raw,depth,validator):
    try:value=decode_json(raw,MAX_MANIFEST)
    except (ValueError,RecursionError,OverflowError):
        raise Refusal('aggregate_shape') from None
    _depth(value,depth)
    _require(next(validator.iter_errors(value),None) is None,'aggregate_shape')
    return value


def _function_state(function):
    function=getattr(function,'__func__',function)
    return function,getattr(function,'__code__',None)


@lru_cache(maxsize=1)
def _memo_manifest_shape(raw,depth,validation_state):
    # Only successful immutable SHAPE DATA is retained. No owner, signature,
    # current/replay, budget or physical-custody result enters this memo.
    return freeze(_parse_manifest_shape(raw,depth,_MANIFEST_VALIDATOR))


def _manifest_shape(raw,depth):
    validator=_MANIFEST_VALIDATOR
    if (validator is not _SHAPE_VALIDATOR or type(validator) is not WireValidator
            or validator.TYPE_CHECKER is not _SHAPE_TYPE_CHECKER
            or validator.format_checker is not _SHAPE_FORMAT_CHECKER
            or validator._resolver is not _SHAPE_RESOLVER):
        return _parse_manifest_shape(raw,depth,validator)
    state=(json.dumps(validator.schema,sort_keys=True,separators=(',',':')),
           _function_state(validator.iter_errors),_function_state(decode_json),
           _function_state(_depth),_function_state(_require),_function_state(_parse_manifest_shape),
           tuple((name,_function_state(function)) for name,function in validator.VALIDATORS.items()))
    # Return an independent mutable working view, never the stored frozen graph.
    return thaw(_memo_manifest_shape(raw,depth,state))


def _verify_original(raw,signature,context):
    """Private algorithm over immutable originals and independent owner context.

    Test factory can supply an unrelated context here; installed production entry
    admits only its fixed independent provider and production policy schema. No
    member bytes, images, transports or effect owner are implemented by U3-B.
    """
    try:
        policy=_owner_policy(context)
        _require(context.placement in ('installer','owner'),'aggregate_scope_mismatch')
        limits=policy['limits']
        _require(type(raw) is bytes and len(raw)<=min(MAX_MANIFEST,limits['manifest_bytes']),'raw_limit')
        if signature is None:raise Refusal('aggregate_unsigned')
        _require(type(signature) is bytes and len(signature)<=min(MAX_SIGNATURE,limits['signature_bytes']),'raw_limit')
        if not signature:raise Refusal('aggregate_unsigned')
        key_id,key=_root(context,policy)
        _require(policy['root']['signature_algorithm']=='ED','aggregate_algorithm_unsupported')
        first,trusted,second=_envelope(signature,key_id)
        _require(verify_ed25519(key,hashlib.blake2b(raw,digest_size=64).digest(),first),'aggregate_signature_invalid')
        _require(verify_ed25519(key,first+trusted,second),'aggregate_signature_invalid')
        value=_manifest_shape(raw,min(32,limits['depth']))
        _require(trusted==_comment(raw,value),'aggregate_scope_mismatch')
        _current(policy,value,context)
        _require(sha256(raw)==policy['expected_aggregate_sha256'],'aggregate_identity_mismatch')
        return ManifestSignature(raw,sha256(raw),sha256(signature),sha256(context.public_root),
                                 policy['root']['key_id'],freeze(value))
    except Refusal:raise
    except Exception:
        raise Refusal('member_authority_unavailable') from None
