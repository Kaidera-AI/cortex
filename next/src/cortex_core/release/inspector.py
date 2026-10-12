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
    value=decode_json(raw,MAX_MANIFEST)
    require(set(value)=={'schema','id','image_digest','members'} and value['schema']=='cortex.inspector-closure.v1' and
            value['id']==profile['inspector'].get('closure_id') and isinstance(value['members'],list) and
            1<=len(value['members'])<=MAX_MEMBERS,'inspector_closure_shape')
    require(isinstance(value['image_digest'],str) and len(value['image_digest'])==71 and value['image_digest'].startswith('sha256:') and
            all(c in '0123456789abcdef' for c in value['image_digest'][7:]),'inspector_image_identity')
    require(bool(candidate_roots),'inspector_candidate_roots_required')
    root=_private_root(root,candidate_roots);members=[];seen=set()
    for record in value['members']:
        require(isinstance(record,dict) and set(record)=={'role','path','bytes','sha256','mode'} and type(record['bytes']) is int and
                type(record['mode']) is int and isinstance(record['sha256'],str) and len(record['sha256'])==64 and
                all(c in '0123456789abcdef' for c in record['sha256']) and record['path'] not in seen and record['role'] in ROLES,'inspector_member_shape')
        seen.add(record['path']);members.append(_read_member(root,record))
    require({r[0] for r in members}==ROLES and sum(r[2] for r in members)<=512*1024*1024,'inspector_execution_closure')
    engines=[r for r in members if r[0]=='engine'];require(len(engines)==1 and engines[0][4]&0o111,'inspector_engine_identity')
    return InspectorClosure(sha256(canonical_bytes(profile)),independently_accepted_sha256,root,tuple(members),value['image_digest'],root/engines[0][1],engines[0][3],_SEAL)


def prepare_plan(capability,profile,request_bytes):
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and
            capability.policy_sha256==sha256(canonical_bytes(profile)) and capability.closure_sha256==profile['inspector'].get('closure_sha256'),'inspector_capability_untrusted')
    spec=profile['inspector'];limits=spec.get('limits',{})
    required={'input_bytes','output_bytes','object_count','depth','seconds','memory_bytes','pids','cpu_millicores'}
    require(set(limits)==required and all(type(v) is int and v>0 for v in limits.values()) and
            limits['input_bytes']<=16*1024*1024 and limits['output_bytes']<=8*1024*1024 and limits['object_count']<=50000 and
            limits['depth']<=128 and limits['seconds']<=120 and limits['memory_bytes']<=512*1024*1024 and limits['pids']<=16 and limits['cpu_millicores']<=2000,'inspector_hard_limits')
    require(type(request_bytes) is bytes and len(request_bytes)<=limits['input_bytes'],'inspector_input_bound')
    require(spec.get('argv')==['/opt/cortex-inspector/bin/python','-I','-S','-B','/opt/cortex-inspector/inspect.py'] and
            spec.get('environment')=={'PYTHONHASHSEED':str(spec.get('hash_seed'))} and type(spec.get('hash_seed')) is int and
            spec.get('optimization') in (0,1,2),'inspector_literal_startup')
    # Recheck immutable closure immediately before preparation/use. No ambient
    # PATH/PYTHONHOME/site hooks, candidate bind mount or host networking.
    for observation in capability.members:
        role,path,size,digest,mode,*_=observation
        current=_read_member(capability.root,dict(role=role,path=path,bytes=size,sha256=digest,mode=mode))
        require(current==observation,'inspector_closure_custody_drift')
    argv=(str(capability.engine),'run','--rm','--pull=never','--network=none','--read-only','--user=65532:65532',
          '--cap-drop=all','--security-opt=no-new-privileges','--pids-limit='+str(limits['pids']),
          '--memory='+str(limits['memory_bytes']),'--memory-swap='+str(limits['memory_bytes']),
          '--cpu-period=100000','--cpu-quota='+str(limits['cpu_millicores']*100),
          '--timeout='+str(limits['seconds']),'--log-driver=none','--interactive',
          '--env','PYTHONHASHSEED='+str(spec['hash_seed']),capability.image_digest,*spec['argv'])
    return InspectorPlan(argv,(),limits['input_bytes'],limits['output_bytes'],limits['seconds'],limits['memory_bytes'],limits['pids'])


def admit_inspection(verified_input,acceptance):
    profile=thaw(verified_input.metadata)['release_policy']
    require(profile.get('schema')=='cortex.release-policy.v1','inspector_profile_unqualified')
    capability=acceptance.get('inspector_capability')
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL,'trusted_inspector_capability_unavailable')
    prepare_plan(capability,profile,b'')
    # Execution/containment needs the independently reviewed platform owner and
    # named native GO. No source/fixture result can grant it in this increment.
    raise ReceiptRefusal('native_inspector_execution_unqualified')
