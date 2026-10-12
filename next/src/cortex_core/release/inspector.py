"""Pre-candidate trust gate and closed non-candidate inspector execution plan.

Native execution remains held: this increment measures/prepares trusted closure,
never launches a process or claims containment/native qualification.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from .codec import canonical_bytes, decode_json, relative_path, sha256
from .contracts import ReceiptRefusal, thaw

_SEAL=object()
MAX_MANIFEST=1024*1024
MAX_MEMBERS=20000
ROLES={'engine','interpreter','stdlib','loader','inspector','image'}


def require(condition,reason):
    if not condition:raise ReceiptRefusal(reason)


@dataclass(frozen=True)
class InspectorClosure:
    policy_sha256:str
    closure_sha256:str
    root:Path
    members:tuple
    image_digest:str
    engine:Path
    engine_sha256:str
    _seal:object


@dataclass(frozen=True)
class InspectorPlan:
    argv:tuple
    environment:tuple
    input_byte_limit:int
    output_byte_limit:int
    cumulative_seconds:int
    memory_bytes:int
    process_limit:int
    native_qualified:bool=False

    def __post_init__(self):
        # Exported typed constructors are not independently verified-image ports.
        raise ReceiptRefusal('inspector_image_member_unbound')


def _private_root(root,candidate_roots):
    root=Path(root)
    require(root.is_absolute(),'inspector_root_untrusted')
    for part in [*reversed(root.parents),root]:
        metadata=part.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and
                metadata.st_uid in (0,os.getuid()) and not metadata.st_mode&0o022,'inspector_root_untrusted')
    resolved=root.resolve(strict=True)
    for candidate in candidate_roots:
        candidate=Path(candidate).resolve(strict=True)
        require(not resolved.is_relative_to(candidate) and not candidate.is_relative_to(resolved),'candidate_inspector_authority')
    return resolved


def _read_member(root,record):
    path=root/relative_path(record['path'])
    for ancestor in path.parents:
        if ancestor==root:break
        metadata=ancestor.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not metadata.st_mode&0o022,'inspector_closure_parent')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid in (0,os.getuid()) and
                not before.st_mode&0o022 and stat.S_IMODE(before.st_mode)==record['mode'] and before.st_size==record['bytes'] and
                0<=before.st_size<=256*1024*1024,'inspector_closure_metadata')
        digest=hashlib.sha256();total=0
        while True:
            chunk=os.read(fd,min(1024*1024,record['bytes']-total+1))
            if not chunk:break
            total+=len(chunk);require(total<=record['bytes'],'inspector_closure_size');digest.update(chunk)
        after=os.fstat(fd);final=path.lstat()
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) and
                (final.st_dev,final.st_ino)==(after.st_dev,after.st_ino) and digest.hexdigest()==record['sha256'],'inspector_closure_changed')
        return (record['role'],record['path'],record['bytes'],record['sha256'],record['mode'],before.st_uid,before.st_dev,before.st_ino,before.st_mtime_ns,before.st_ctime_ns)
    finally:os.close(fd)


def admit_closure(raw,independently_accepted_sha256,profile,root,*,candidate_roots):
    """Original registry asset bytes are accepted before decode or physical reads.

    The independently supplied identity must be from the trusted install/release
    owner, not candidate output. This is asset custody, not a second rules policy.
    """
    require(type(raw) is bytes and len(raw)<=MAX_MANIFEST and sha256(raw)==independently_accepted_sha256 and
            profile['inspector'].get('closure_sha256')==independently_accepted_sha256,'inspector_closure_not_accepted')
    # A measured local manifest and its claimed digest do not establish image
    # contents, invoked member paths, or ABI. No independent verified-image port
    # exists in this increment. Refuse before decode, member reads, or plan minting.
    raise ReceiptRefusal('inspector_image_member_unbound')


def startup_argv(profile):
    """Narrow isolated startup policy; no ineffective PYTHON* setting promises."""
    spec=profile['inspector']
    require(spec.get('argv')==['/opt/cortex-inspector/bin/python','-I','-S','-B','/opt/cortex-inspector/inspect.py'] and
            spec.get('environment')=={} and 'hash_seed' not in spec and
            type(spec.get('optimization')) is int and spec['optimization']==0,'inspector_literal_startup')
    return tuple(spec['argv'])


def prepare_plan(capability,profile,request_bytes):
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and
            capability.policy_sha256==sha256(canonical_bytes(profile)) and capability.closure_sha256==profile['inspector'].get('closure_sha256'),'inspector_capability_untrusted')
    # Even an in-process constructed/sealed closure carries no image proof.
    # No plan object may be minted until the independent port is implemented.
    raise ReceiptRefusal('inspector_image_member_unbound')


def admit_inspection(verified_input,acceptance):
    profile=thaw(verified_input.metadata)['release_policy']
    require(profile.get('schema')=='cortex.release-policy.v1','inspector_profile_unqualified')
    capability=acceptance.get('inspector_capability')
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL,'trusted_inspector_capability_unavailable')
    _require_verified_image(capability)
    # Execution/containment needs the independently reviewed platform owner and
    # named native GO. No source/fixture result can grant it in this increment.
    raise ReceiptRefusal('native_inspector_execution_unqualified')


def _require_verified_image(capability):
    """No independent image/member/path/ABI verifier exists in this increment."""
    raise ReceiptRefusal('inspector_image_member_unbound')
