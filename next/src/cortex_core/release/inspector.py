"""Pre-candidate trust gate, verified-image port and one contained inspector run.

R3 (accepted note v2): the owner-accepted closure DESCRIPTOR names an inspector
OCI archive; its members, closed config and CPython identity are proven from the
archive's verified layers before any candidate reader. The run is one rootless
container with one fixed argv, live output caps and forced owned cleanup.
Native execution stays unqualified (R3-T2): host eligibility plus a sealed
closure are prerequisites only, until admitted R6 evidence exists.
"""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
from .codec import canonical_bytes, decode_json, relative_path, sha256
from .contracts import ReceiptRefusal, freeze, thaw

_SEAL=object()
_ADMITTED=object()
MAX_MANIFEST=1024*1024
MAX_MEMBERS=20000
ROLES={'engine','interpreter','stdlib','loader','inspector','image'}
STARTUP=('/opt/cortex-inspector/bin/python','-I','-S','-B','/opt/cortex-inspector/inspect.py')
CLOSED_CONFIG={'User':'65534:65534','WorkingDir':'/'}
CLEANUP_SECONDS=30
KILL_SECONDS=CLEANUP_SECONDS/3
# The one fixed engine process environment (names); R01 binds every child to it.
ENGINE_ENVIRONMENT=('CONTAINERS_CONF','CONTAINERS_STORAGE_CONF','HOME','PATH','TMPDIR','XDG_RUNTIME_DIR')
ARCH={'linux/amd64':('amd64','x86_64'),'linux/arm64':('arm64','aarch64')}
DESCRIPTOR_KEYS={'schema','id','platform','python','archive','manifest_sha256','config_sha256',
                 'symlinks','members','engine','engine_config'}
FILE_KEYS={'path','bytes','sha256','mode'}


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
    descriptor:object=field(default=None,compare=False,repr=False)
    _admission:object=field(default=None,compare=False,repr=False)


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
    session:Path=None
    name:str=None
    prefix:tuple=None
    request_sha256:str=None
    _admission:object=field(default=None,compare=False,repr=False)

    def __post_init__(self):
        # Only prepare_plan mints a plan, and only from an admitted closure.
        if self._admission is not _ADMITTED or self.native_qualified is not False:
            raise ReceiptRefusal('inspector_image_member_unbound')


def _now():
    import time
    return time.monotonic()


def _budget(seconds):
    """One work deadline + one cleanup reserve (Hale H2), as the shared engine Deadline."""
    from .build.engine import Deadline
    return Deadline(seconds+CLEANUP_SECONDS,clock=_now,reserve=CLEANUP_SECONDS)


def _work_timeout(deadline):
    # the child's own TERM/KILL wait must fit inside the same window (Hale R4-BUDGET-001)
    try:return deadline.child_timeout(KILL_SECONDS)
    except TimeoutError:raise ReceiptRefusal('inspector_run_incomplete') from None


@dataclass(frozen=True)
class InspectionSession:
    """The one capability vfs may use to judge CHECKED_HASH caches."""
    capability:InspectorClosure
    profile:object
    _admission:object=field(default=None,compare=False,repr=False)
    budget:object=field(default=None,compare=False,repr=False)

    def check(self,rows):
        require(self._admission is _ADMITTED and isinstance(self.capability,InspectorClosure),'inspector_capability_untrusted')
        profile=thaw(self.profile)
        verdicts=_inspect(self.capability,profile,rows,self.budget)[1]
        require(all(equal is True for equal in verdicts),'python_compilation_policy')


def _request(capability,profile,rows):
    """The one canonical request for a set of controller-checked cache rows."""
    import base64
    from . import _pyc_check
    spec=profile['inspector'];python=thaw(capability.descriptor)['descriptor']['python']
    order=[row['cache'] for row in rows]
    require(bool(order) and order==sorted(set(order)) and all(row['optimization']==spec['optimization'] for row in rows),
            'inspector_run_incomplete')
    request=canonical_bytes({'schema':_pyc_check.REQUEST_SCHEMA,'policy_sha256':capability.policy_sha256,
        'closure_sha256':capability.closure_sha256,'abi':{'version':python['version'],'magic':python['magic']},
        'optimization':spec['optimization'],
        'rows':[{'cache':row['cache'],'source':row['source'],'final_filename':row['final_filename'],
                 'cache_b64':base64.b64encode(row['cache_bytes']).decode('ascii'),
                 'source_b64':base64.b64encode(row['source_bytes']).decode('ascii')} for row in rows]})
    return request,order


def _inspect(capability,profile,rows,budget,account=None,children=None):
    """Request -> plan -> contained run -> request-bound verdicts (the one transport)."""
    request,order=_request(capability,profile,rows)
    plan=prepare_plan(capability,profile,request)
    children=children if children is not None else _Children(dict(plan.environment))
    reply=run_inspection(capability,plan,request,budget=budget,account=account,children=children)
    verdicts=_reply_verdicts(reply,request,thaw(capability.descriptor)['descriptor'],order,profile['inspector']['limits'])
    return {'closure_sha256':capability.closure_sha256,'request_sha256':sha256(request),'reply_sha256':sha256(reply),
            'argv':list(plan.argv),'children':children.records,
            'verdicts':[{'cache':cache,'equal':equal} for cache,equal in zip(order,verdicts)]},verdicts


def _inspector_purpose(capability):
    require(isinstance(capability,InspectorClosure) and thaw(capability.descriptor).get('purpose')=='inspector',
            'inspector_capability_untrusted')


def run_probe(capability,profile,probe,*,deadline=None,account=None,evidence_dir=None):
    """R6: one containment probe through the SAME plan and transport with the admitted
    purpose=probe asset. Returns its request, reply (None when stopped), error and the
    real child records; it never yields a session or an inspector verdict."""
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and capability._admission is _ADMITTED and
            thaw(capability.descriptor).get('purpose')=='probe','inspector_capability_untrusted')
    request=canonical_bytes({'schema':'cortex.probe-request.v1','probe':probe})
    plan=prepare_plan(capability,profile,request)
    children=_Children(dict(plan.environment),evidence_dir,deadline is not None)
    budget=deadline or _budget(profile['inspector']['limits']['seconds'])
    reply=error=None
    try:reply=run_inspection(capability,plan,request,budget=budget,account=account,children=children)
    except ReceiptRefusal as refusal:error=str(refusal)
    return {'probe':probe,'request':request,'reply':reply,'error':error,'argv':list(plan.argv),'children':children.records}


def collect_inspection_evidence(capability,profile,rows,*,deadline=None,account=None,evidence_dir=None,history=None):
    """EVIDENCE-ONLY producer call (Vera R4-T1): the same transport and core as the
    session, under an admitted closure, returning measured unqualified evidence.

    It never calls admit_inspection or _session, never takes or returns an
    InspectionSession, and marks nothing qualified. An unequal cache is a finding
    in the returned record, not an exception that could read like success.
    """
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and capability._admission is _ADMITTED,
            'inspector_capability_untrusted')
    _inspector_purpose(capability)
    nested=deadline is not None
    budget=deadline or _budget(profile['inspector']['limits']['seconds'])
    request,order=_request(capability,profile,rows)
    plan=prepare_plan(capability,profile,request)
    children=_Children(dict(plan.environment),evidence_dir,nested)
    if history is not None:
        # the caller's list IS the collector's record list: a failed run keeps its history
        children.records=history
    reply=run_inspection(capability,plan,request,budget=budget,account=account,children=children)
    verdicts=_reply_verdicts(reply,request,thaw(capability.descriptor)['descriptor'],order,profile['inspector']['limits'])
    return {'closure_sha256':capability.closure_sha256,'request_sha256':sha256(request),'reply_sha256':sha256(reply),
            'request':request,'reply':reply,'argv':list(plan.argv),'children':children.records,
            'verdicts':[{'cache':cache,'equal':equal} for cache,equal in zip(order,verdicts)]}


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


def _read_member(root,record,*,keep=False):
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
        digest=hashlib.sha256();total=0;body=[]
        while True:
            chunk=os.read(fd,min(1024*1024,record['bytes']-total+1))
            if not chunk:break
            total+=len(chunk);require(total<=record['bytes'],'inspector_closure_size');digest.update(chunk)
            if keep:body.append(chunk)
        after=os.fstat(fd);final=path.lstat()
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) and
                (final.st_dev,final.st_ino)==(after.st_dev,after.st_ino) and digest.hexdigest()==record['sha256'],'inspector_closure_changed')
        row=(record['role'],record['path'],record['bytes'],record['sha256'],record['mode'],before.st_uid,before.st_dev,before.st_ino,before.st_mtime_ns,before.st_ctime_ns)
        return (row,b''.join(body)) if keep else row
    finally:os.close(fd)


def _hex(value):
    return type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _file_row(row,absolute):
    return (type(row) is dict and set(row)==FILE_KEYS and type(row['path']) is str and
            (row['path'].startswith('/') and relative_path(row['path'][1:])==row['path'][1:] if absolute
             else relative_path(row['path'])==row['path']) and
            type(row['bytes']) is int and 0<=row['bytes']<=256*1024*1024 and _hex(row['sha256']) and
            type(row['mode']) is int and 0<=row['mode']<=0o7777)


def _descriptor(value,spec,profile,script=None):
    """Closed descriptor shape; the pre-reader ABI identity is read here (Vera)."""
    from . import _pyc_check
    try:
        valid=(type(value) is dict and set(value)==DESCRIPTOR_KEYS and value['schema']=='cortex.inspector-closure.v1' and
               value['id']==spec['closure_id'] and value['platform'] in ARCH and
               value['platform']==profile['target']['oci_platform'] and _file_row(value['archive'],False) and
               _hex(value['manifest_sha256']) and _hex(value['config_sha256']) and
               type(value['symlinks']) is dict and all(type(k) is str and type(v) is str for k,v in value['symlinks'].items()) and
               _file_row(value['engine'],True) and type(value['engine_config']) is dict and
               set(value['engine_config'])=={'containers_conf','storage_conf'} and
               all(_file_row(row,True) for row in value['engine_config'].values()) and
               type(value['members']) is list and 0<len(value['members'])<=MAX_MEMBERS)
        rows=value['members'] if valid else []
        valid=valid and all(type(r) is dict and set(r)==FILE_KEYS|{'role'} and r['role'] in ROLES-{'engine','image'} and
                            _file_row({k:r[k] for k in FILE_KEYS},True) for r in rows)
        paths=[r['path'] for r in rows] if valid else []
        roles=[r['role'] for r in rows] if valid else []
        script=Path(_pyc_check.__file__).read_bytes() if script is None else script
        valid=(valid and len(set(paths))==len(paths) and roles.count('interpreter')==1 and roles.count('inspector')==1 and
               'stdlib' in roles and 'loader' in roles and
               any(r['role']=='interpreter' and r['path']==STARTUP[0] for r in rows) and
               any(r['role']=='inspector' and r['path']==STARTUP[4] and r['sha256']==sha256(script) and r['bytes']==len(script) for r in rows))
    except (KeyError,TypeError,ValueError):
        valid=False
    require(valid,'inspector_image_member_unbound')
    python=value['python']
    require(type(python) is dict and set(python)=={'version','magic'} and type(python['version']) is str and
            len(python['version'].split('.'))==3 and all(p.isdigit() for p in python['version'].split('.')) and
            _pyc_check.MAGIC.get(python['version'].rsplit('.',1)[0])==python['magic'],'inspector_abi_mismatch')
    return value


def _host_file(row,role,candidate_roots):
    path=Path(row['path'])
    return _read_member(_private_root(path.parent,candidate_roots),dict(row,role=role,path=path.name))


def _parents(path):
    parts=path.split('/')
    return ['/'.join(parts[:n]) for n in range(1,len(parts))]


def admit_closure(raw,independently_accepted_sha256,profile,root,*,candidate_roots,script=None):
    """Original registry asset bytes are accepted before decode or physical reads.

    The independently supplied identity must be from the trusted install/release
    owner, not candidate output. It names the descriptor only (Hale H4); the
    archive, manifest M and config/ImageID C are separate rows inside it.
    """
    # the profile names the product inspector only; the test-only probe asset's sha is
    # accepted separately by its owner (R6-ASSET-001) and supplied by the caller
    require(type(raw) is bytes and len(raw)<=MAX_MANIFEST and sha256(raw)==independently_accepted_sha256 and
            (script is not None or profile['inspector'].get('closure_sha256')==independently_accepted_sha256),
            'inspector_closure_not_accepted')
    spec=profile['inspector']
    # A profile that does not request contained native execution binds no image.
    require(spec.get('execution')=='contained' and spec.get('scope')=='native' and spec.get('python_caches')=='CHECKED_HASH' and
            type(spec.get('closure_id')) is str and type(spec.get('limits')) is dict,'inspector_image_member_unbound')
    startup_argv(profile)
    try:value=decode_json(raw,MAX_MANIFEST)
    except ValueError:raise ReceiptRefusal('inspector_image_member_unbound') from None
    # R6-ASSET-001: the same port admits the test-only probe asset when the caller names
    # its script bytes; the result is purpose=probe and no session path accepts it.
    purpose='inspector' if script is None else 'probe'
    from . import _pyc_check
    require(script is None or (type(script) is bytes and script!=Path(_pyc_check.__file__).read_bytes()),
            'inspector_image_member_unbound')  # the real inspector is never a probe asset
    value=_descriptor(value,spec,profile,script)
    private=_private_root(root,candidate_roots)
    engine=_host_file(value['engine'],'engine',candidate_roots)
    for name,row in sorted(value['engine_config'].items()):_host_file(row,'engine',candidate_roots)
    _,archive=_read_member(private,dict(value['archive'],role='image'),keep=True)
    from .artifacts import oci_image,oci_layers,tar_members
    try:
        _,blobs,manifest,manifest_key,config,config_key=oci_image(archive,len(archive),value['platform'])
        layers,_=oci_layers(manifest,config,blobs,manifest_key,config_key)
        require(manifest_key==value['manifest_sha256'] and config_key==value['config_sha256'],'inspector_image_member_unbound')
        # Vera R3-T1: the closed config fixes the first process before any read or run.
        require(config.get('config')==CLOSED_CONFIG,'inspector_image_member_unbound')
        files={}
        for key in layers:
            _,rows=tar_members(blobs[key],len(archive),metadata={'native_v1':True},
                               layer_policy={'allow_directories':True,'symlinks':value['symlinks']},preserve_order=True)
            for row in rows:
                require(not any(part.startswith('.wh.') for part in row['path'].split('/')),'inspector_image_member_unbound')
                files[row['path']]=row
    except ReceiptRefusal:
        raise ReceiptRefusal('inspector_image_member_unbound') from None
    for member in value['members']:
        path=member['path'][1:];found=files.get(path)
        executable=member['role'] in ('interpreter','loader')
        require(found is not None and found.get('kind','regular')=='regular' and
                (found['bytes'],found['sha256'],found['mode'])==(member['bytes'],member['sha256'],member['mode']) and
                found['mode']&(0o005 if executable else 0o004)==(0o005 if executable else 0o004) and
                all(parent not in files or (files[parent].get('kind')=='directory' and files[parent]['mode']&0o001)
                    for parent in _parents(path)),'inspector_image_member_unbound')
    record=freeze({'descriptor':value,'diff_ids':['sha256:'+key for key in layers],'purpose':purpose})
    return InspectorClosure(sha256(canonical_bytes(profile)),independently_accepted_sha256,private,
                            tuple((m['role'],m['path'],m['sha256']) for m in value['members']),'sha256:'+config_key,
                            Path(value['engine']['path']),engine[3],_SEAL,record,_ADMITTED)


def startup_argv(profile):
    """Narrow isolated startup policy; no ineffective PYTHON* setting promises."""
    spec=profile['inspector']
    require(spec.get('argv')==['/opt/cortex-inspector/bin/python','-I','-S','-B','/opt/cortex-inspector/inspect.py'] and
            spec.get('environment')=={} and 'hash_seed' not in spec and
            type(spec.get('optimization')) is int and spec['optimization']==0,'inspector_literal_startup')
    return tuple(spec['argv'])


def _shape(value):
    """Count every JSON value (root included) and the depth (root = 1)."""
    count,depth,pending=0,0,[(value,1)]
    while pending:
        item,level=pending.pop();count+=1;depth=max(depth,level)
        if type(item) is dict:pending.extend((child,level+1) for child in item.values())
        elif type(item) is list:pending.extend((child,level+1) for child in item)
    return count,depth


def prepare_plan(capability,profile,request_bytes):
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and
            capability.policy_sha256==sha256(canonical_bytes(profile)) and
            (capability.closure_sha256==profile['inspector'].get('closure_sha256') or
             thaw(capability.descriptor).get('purpose')=='probe'),'inspector_capability_untrusted')  # R6 probe: own accepted sha
    # A sealed object alone is not a verified image: only admit_closure admits.
    require(capability._admission is _ADMITTED,'inspector_image_member_unbound')
    import secrets
    from .build.engine import cpu_memory_argv
    from . import _pyc_check
    limits=profile['inspector']['limits'];record=thaw(capability.descriptor)['descriptor']
    require(type(request_bytes) is bytes and len(request_bytes)<=limits['input_bytes'],'inspector_run_incomplete')
    try:count,depth=_shape(decode_json(request_bytes,limits['input_bytes']))
    except ValueError:raise ReceiptRefusal('inspector_run_incomplete') from None
    require(count<=limits['object_count'] and depth<=limits['depth'],'inspector_run_incomplete')
    session=capability.root/('session-'+secrets.token_hex(16))
    require(not any(c in str(session) for c in ',=\n\r\x00'),'inspector_root_untrusted')
    name='cortex-inspector-'+secrets.token_hex(16)
    argv=plan_argv(str(capability.engine),capability.image_digest,limits,session,name)
    prefix=argv[:8]
    environment=(('CONTAINERS_CONF',record['engine_config']['containers_conf']['path']),
                 ('CONTAINERS_STORAGE_CONF',record['engine_config']['storage_conf']['path']),
                 ('HOME',str(session/'home')),('PATH','/usr/bin:/bin'),('TMPDIR',str(session/'tmp')),
                 ('XDG_RUNTIME_DIR',str(session/'xdg')))
    return InspectorPlan(argv,environment,limits['input_bytes'],limits['output_bytes'],limits['seconds'],
                         limits['memory_bytes'],limits['pids'],False,session,name,prefix,sha256(request_bytes),_ADMITTED)


def plan_argv(engine,image,limits,session,name):
    """The ONE contained-run argv (R3 H1). R01 rebuilds it from a journal's session and
    name to bind qualification evidence to the actual plan, flags and limits."""
    from .build.engine import cpu_memory_argv
    from . import _pyc_check
    session=Path(session)
    return (engine,'--remote=false','--root',str(session/'store'),'--runroot',str(session/'run'),
            '--tmpdir',str(session/'tmp'),'run','--name',name,'--cidfile',str(session/'cid'),'--pull=never','--rm',
            '--read-only','--read-only-tmpfs=false','--image-volume=ignore','--no-healthcheck','--log-driver=none',
            '--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--user',CLOSED_CONFIG['User'],
            '--workdir=/','--unsetenv-all','--pids-limit',str(limits['pids']),
            *cpu_memory_argv(limits['cpu_millicores'],limits['memory_bytes']),'--memory-swap',str(limits['memory_bytes']),
            '--mount','type=bind,src='+str(session/'request'/'request.json')+',dst='+_pyc_check.REQUEST_PATH+
            ',ro=true,relabel=private','--entrypoint',STARTUP[0],image,*STARTUP[1:])


class _Children:
    """Records every engine child of one run (failed attempts included) with full
    metadata; streams are copied into evidence_dir before the session is removed."""

    def __init__(self,environment,evidence_dir=None,nested=False):
        self.environment,self.evidence_dir,self.nested,self.records=environment,evidence_dir,nested,[]

    def call(self,argv,session,timeout,limit):
        import shutil,time
        started=time.time_ns();row={'argv':list(argv),'cwd':str(session),'environment_names':sorted(self.environment),
                                     'started_at_unix_ns':started,'timeout_ns':int(timeout*10**9)}
        try:result=_engine(argv,self.environment,session,timeout,limit)
        except BaseException as error:
            row.update(exit=None,error=type(error).__name__,finished_at_unix_ns=time.time_ns(),
                       stdout=None,stderr=None,journal=None)
            self.records.append(row)
            raise
        # (exit, stdout[, files[, error]]): a failed adapter run (R6-FU-1) hands back its
        # REAL journal/streams with the error; they are kept, then the error is raised
        code,stdout,files,failure=(*result,None,None)[:4]
        row.update(exit=None if failure is not None else code,finished_at_unix_ns=time.time_ns())
        if failure is not None:row['error']=type(failure).__name__
        for key in ('stdout','stderr','journal'):
            source=(files or {}).get(key)
            if source is not None and self.evidence_dir is not None:
                target=Path(self.evidence_dir)/('r3-'+str(len(self.records))+'.'+key)
                shutil.copyfile(source,target)
                raw=target.read_bytes()
                row[key]={'path':str(target),'bytes':len(raw),'sha256':sha256(raw)}
            elif key=='stdout':
                row[key]={'path':None,'bytes':len(stdout),'sha256':sha256(stdout)}
            else:
                row[key]=None
        self.records.append(row)
        if failure is not None:raise failure
        return code,stdout


def _engine(argv,environment,session,timeout,output_limit):
    """One engine operation through the one adapter; returns (exit, stdout, files)."""
    import secrets
    from .build.engine import OutputAccount,run_operation
    stem=session/('op-'+secrets.token_hex(8))
    # output_limit is a per-call byte cap, or a lane's shared OutputAccount (R4 nesting)
    budget={'account':output_limit} if isinstance(output_limit,OutputAccount) else {'output_limit':output_limit}
    out,err,journal=Path(str(stem)+'.out'),Path(str(stem)+'.err'),Path(str(stem)+'.out.operation.jsonl')
    try:
        result=run_operation(argv,session,dict(environment),timeout,out,err,kill_seconds=KILL_SECONDS,**budget)
    except Exception as error:
        # the adapter wrote its failed journal before raising: hand it to the collector
        if not (out.is_file() and err.is_file() and journal.is_file()):raise
        return None,out.read_bytes(),{'stdout':out,'stderr':err,'journal':journal},error
    return result['exit'],out.read_bytes(),{'stdout':out,'stderr':err,'journal':Path(result['journal'])}


def _same_root(roots,session):
    """The session path still names the directory created and recorded at the start."""
    try:
        (path,expected),=roots.created.items()
        return path==session.resolve() and roots.identity(session.stat(follow_symlinks=False))==expected
    except (OSError,ValueError):
        return False


def _write_owned(path,raw,mode):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        view=memoryview(raw)
        while view:view=view[os.write(fd,view):]
        os.fchmod(fd,mode);os.fsync(fd)
    finally:os.close(fd)


def _loaded(raw,record,capability):
    """Independent inspect of the fresh store: Id = C, platform and diff_ids."""
    try:value=decode_json(raw,1024*1024,canonical=False)
    except ValueError:raise ReceiptRefusal('inspector_run_incomplete') from None
    descriptor=record['descriptor']
    require(type(value) is list and len(value)==1 and type(value[0]) is dict and
            value[0].get('Id') in (descriptor['config_sha256'],'sha256:'+descriptor['config_sha256']) and
            value[0].get('Os')=='linux' and value[0].get('Architecture')==ARCH[descriptor['platform']][0] and
            type(value[0].get('RootFS')) is dict and value[0]['RootFS'].get('Layers')==record['diff_ids'],'inspector_run_incomplete')


def run_inspection(capability,plan,request_bytes,*,budget=None,account=None,children=None):
    """Fresh owned store, load the custody copy, inspect, run, always clean up."""
    from .build.engine import OwnedRoots
    require(isinstance(plan,InspectorPlan) and plan._admission is _ADMITTED and type(request_bytes) is bytes and
            sha256(request_bytes)==plan.request_sha256 and plan.session.parent==capability.root,'inspector_capability_untrusted')
    record=thaw(capability.descriptor);descriptor=record['descriptor']
    environment=dict(plan.environment);session=plan.session
    budget=budget or _budget(plan.cumulative_seconds)
    remaining=lambda:_work_timeout(budget)
    children=children if children is not None else _Children(environment)
    roots=None;inner={}
    try:
        try:
            remaining()
            roots=OwnedRoots([session]).create()
            for name in ('store','run','tmp','home','xdg','request'):os.mkdir(session/name,0o700)
            for name in ('store','run'):
                held=(session/name).stat(follow_symlinks=False);inner[name]=(held.st_dev,held.st_ino)
            os.chmod(session/'request',0o755)
            _write_owned(session/'request'/'request.json',request_bytes,0o444)
            _,archive=_read_member(capability.root,dict(descriptor['archive'],role='image'),keep=True)
            _write_owned(session/'image.tar',archive,0o444)
            for row in [descriptor['engine'],*descriptor['engine_config'].values()]:_host_file(row,'engine',())
            code,_=children.call(plan.prefix+('load','--input',str(session/'image.tar')),session,remaining(),account or 64*1024)
            require(code==0,'inspector_run_incomplete')
            code,raw=children.call(plan.prefix+('image','inspect','--format','json',capability.image_digest),
                                   session,remaining(),account or 1024*1024)
            require(code==0,'inspector_run_incomplete');_loaded(raw,record,capability)
            code,reply=children.call(plan.argv,session,remaining(),account or plan.output_byte_limit)
            require(code==0,'inspector_run_incomplete')
            _read_member(session/'request',dict(role='image',path='request.json',bytes=len(request_bytes),
                                                 sha256=plan.request_sha256,mode=0o444))
            return reply
        except ReceiptRefusal:raise
        except Exception:raise ReceiptRefusal('inspector_run_incomplete') from None
    finally:
        if roots is not None:
            _cleanup(plan,environment,session,budget=budget,roots=roots,inner=inner,account=account,children=children)


def _cleanup(plan,environment,session,*,budget=None,roots=None,inner=None,account=None,children=None):
    """Remove the owned container by name and prove it absent; only then the owned roots.

    Unproven absence or a replaced session directory preserves every root for
    accountable recovery. All waits come from the one cleanup reserve.
    """
    budget=budget or _budget(0)
    children=children if children is not None else _Children(environment)
    def operation(argv):
        # a nested run (inside a lane phase) cleans up inside that phase window
        timeout=(budget.child_timeout(KILL_SECONDS) if children.nested else budget.cleanup_timeout(KILL_SECONDS))
        if timeout<=0:raise TimeoutError('cleanup reserve spent')
        return children.call(argv,session,timeout,account or 64*1024)
    # Hale R4-OWN-001 rule, here too: prove the recorded root before any engine command.
    require(roots is not None and _same_root(roots,session),'inspector_run_incomplete')
    try:
        code,_=operation(plan.prefix+('rm','--force','--time','0','--ignore',plan.name))
        absent=code==0
        code,out=operation(plan.prefix+('ps','-a','--filter','name=^'+plan.name+'$','--format','{{.ID}}'))
        absent=absent and code==0 and out.strip()==b''
        cid=session/'cid'
        if cid.exists():
            value=cid.read_bytes().strip()
            absent=absent and len(value)==64 and all(c in b'0123456789abcdef' for c in value)
    except Exception:
        absent=False
    require(absent and roots is not None and _same_root(roots,session) and
            type(inner) is dict and set(inner)=={'store','run'},'inspector_run_incomplete')
    try:
        # Rootless store files belong to mapped ids: the one descriptor-safe removal runs
        # inside the namespace and checks each recorded identity in the deleting process.
        import sys
        from .build import _owned_remove
        targets=[str(part) for name in ('store','run') for part in (session/name,*inner[name])]
        code,_=operation(plan.prefix+('unshare',sys.executable,'-I','-S','-B',_owned_remove.__file__,*targets))
        removed=code==0
    except Exception:
        removed=False
    require(removed and _same_root(roots,session),'inspector_run_incomplete')
    result=roots.cleanup()
    require(result['complete'] is True and result['remaining_owned_resources']==[] and not session.exists(),
            'inspector_run_incomplete')


def _reply_verdicts(reply,request,descriptor,order,limits):
    """Closed reply bound to this exact request: every cache once, in order."""
    from . import _pyc_check
    try:value=decode_json(reply,limits['output_bytes'])
    except ValueError:raise ReceiptRefusal('inspector_run_incomplete') from None
    count,depth=_shape(value)
    require(count<=limits['object_count'] and depth<=limits['depth'],'inspector_run_incomplete')
    require(type(value) is dict and set(value)=={'schema','request_sha256','abi','verdicts'} and
            value['schema']==_pyc_check.RESPONSE_SCHEMA and value['request_sha256']==sha256(request),'inspector_run_incomplete')
    require(value['abi']=={'version':descriptor['python']['version'],'magic':descriptor['python']['magic'],
                           'machine':ARCH[descriptor['platform']][1]},'inspector_abi_mismatch')
    verdicts=value['verdicts']
    require(type(verdicts) is list and len(verdicts)==len(order) and
            all(type(row) is dict and set(row)=={'cache','equal'} and row['cache']==cache and type(row['equal']) is bool
                for row,cache in zip(verdicts,order)),'inspector_run_incomplete')
    return [row['equal'] for row in verdicts]


def _session(capability,profile):
    """Mint the cache-judging session for an admitted closure (R6 gates its use)."""
    require(isinstance(capability,InspectorClosure) and capability._admission is _ADMITTED and
            thaw(capability.descriptor).get('purpose')=='inspector','trusted_inspector_capability_unavailable')
    return InspectionSession(capability,freeze(profile),_ADMITTED,_budget(profile['inspector']['limits']['seconds']))


@dataclass(frozen=True)
class _Pending:
    """Eligibility only (closure, ABI, host): no session, no cache judging."""
    capability:InspectorClosure
    profile:object
    host:object
    _admission:object=field(default=None,compare=False,repr=False)
    probe:object=None  # R6: the accepted purpose=probe asset (same policy and engine), or None
    allowlist:object=None  # R6: the ACCEPTED mounts/environment allowlist (tate R8), or None


def preflight(verified_input,acceptance):
    """Before any candidate reader: admitted closure, its ABI vs the toolchain, R8 host."""
    profile=thaw(verified_input.metadata)['release_policy']
    require(profile.get('schema')=='cortex.release-policy.v1','inspector_profile_unqualified')
    capability=acceptance.get('inspector_capability')
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and capability._admission is _ADMITTED and
            capability.policy_sha256==sha256(canonical_bytes(profile)) and
            thaw(capability.descriptor).get('purpose')=='inspector' and
            capability.closure_sha256==profile['inspector'].get('closure_sha256'),'trusted_inspector_capability_unavailable')
    value=thaw(verified_input.document);descriptor=thaw(capability.descriptor)['descriptor']
    version='.'.join(value['builder_requirements']['toolchain']['version'].split('.')[:2])
    # Vera ABI rule: the admitted closure's CPython identity, before any candidate reader.
    require(descriptor['python']['version'].rsplit('.',1)[0]==version and
            descriptor['platform']==value['target']['oci_platform'],'inspector_abi_mismatch')
    from .build.policy import verify_native
    host=acceptance.get('inspector_host') or {}
    try:verify_native(host,descriptor['platform'])
    except ValueError:raise ReceiptRefusal('native_inspector_execution_unqualified') from None
    # Cox R4-R01-002: only a native-scope, native-qualified input can ever reach a session.
    try:qualified=verified_input.native_qualified is True
    except AttributeError:qualified=False
    require(qualified and (value.get('admission_policy') or {}).get('qualification_scope')=='native',
            'native_inspector_execution_unqualified')
    # R6 context comes from the acceptance policy only, never from the producer's record.
    probe=acceptance.get('probe_capability')
    if not (isinstance(probe,InspectorClosure) and probe._seal is _SEAL and probe._admission is _ADMITTED and
            thaw(probe.descriptor).get('purpose')=='probe' and probe.policy_sha256==capability.policy_sha256 and
            probe.engine==capability.engine and probe.engine_sha256==capability.engine_sha256):
        probe=None
    allowlist=acceptance.get('containment_allowlist')
    if not (type(allowlist) is dict and set(allowlist)=={'mounts','environment'} and
            all(type(v) is list and all(type(x) is str for x in v) for v in allowlist.values())):
        allowlist=None
    return _Pending(capability,freeze(profile),freeze(host),_ADMITTED,probe,freeze(allowlist))


def admit_session(pending,matched):
    """The ONE native session, only after R01 matched the measured R6 qualification records."""
    from .receipts import MatchedQualification
    from .contracts import _SEAL as R01_SEAL
    require(isinstance(pending,_Pending) and pending._admission is _ADMITTED,'trusted_inspector_capability_unavailable')
    # the match must belong to THIS verification's pending context, not just its closure
    require(isinstance(matched,MatchedQualification) and matched._seal is R01_SEAL and matched.pending is pending and
            matched.closure_sha256==pending.capability.closure_sha256,'native_inspector_execution_unqualified')
    return _session(pending.capability,thaw(pending.profile))


def admit_inspection(verified_input,acceptance):
    """Legacy single entry (CLI): eligibility, then still unqualified without matched R6 records."""
    preflight(verified_input,acceptance)
    raise ReceiptRefusal('native_inspector_execution_unqualified')
