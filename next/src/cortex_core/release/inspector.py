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


class _Budget:
    """One work deadline and one cleanup reserve for a whole session (Hale H2).

    Every operation timeout and its TERM/KILL allowance is drawn from these two
    absolute instants; nothing restarts per batch, operation or pyc. The hard
    ceiling is limits.seconds + CLEANUP_SECONDS from the session's start.
    """

    def __init__(self,seconds):
        import time
        now=time.monotonic()
        self.work=now+seconds
        self.total=now+seconds+CLEANUP_SECONDS

    def work_timeout(self):
        # A work operation's own TERM/KILL wait falls inside the reserve that follows self.work.
        import time
        left=self.work-time.monotonic()
        require(left>0,'inspector_run_incomplete')
        return left

    def cleanup_timeout(self):
        import time
        return self.total-time.monotonic()-KILL_SECONDS


@dataclass(frozen=True)
class InspectionSession:
    """The one capability vfs may use to judge CHECKED_HASH caches."""
    capability:InspectorClosure
    profile:object
    _admission:object=field(default=None,compare=False,repr=False)
    budget:object=field(default=None,compare=False,repr=False)

    def check(self,rows):
        require(self._admission is _ADMITTED and isinstance(self.capability,InspectorClosure),'inspector_capability_untrusted')
        profile=thaw(self.profile);spec=profile['inspector']
        record=thaw(self.capability.descriptor);python=record['descriptor']['python']
        order=[row['cache'] for row in rows]
        require(bool(order) and order==sorted(set(order)) and all(row['optimization']==spec['optimization'] for row in rows),
                'inspector_run_incomplete')
        import base64
        from . import _pyc_check
        request=canonical_bytes({'schema':_pyc_check.REQUEST_SCHEMA,'policy_sha256':self.capability.policy_sha256,
            'closure_sha256':self.capability.closure_sha256,'abi':{'version':python['version'],'magic':python['magic']},
            'optimization':spec['optimization'],
            'rows':[{'cache':row['cache'],'source':row['source'],'final_filename':row['final_filename'],
                     'cache_b64':base64.b64encode(row['cache_bytes']).decode('ascii'),
                     'source_b64':base64.b64encode(row['source_bytes']).decode('ascii')} for row in rows]})
        plan=prepare_plan(self.capability,profile,request)
        verdicts=_reply_verdicts(run_inspection(self.capability,plan,request,budget=self.budget),request,record['descriptor'],order,spec['limits'])
        require(all(equal is True for equal in verdicts),'python_compilation_policy')


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


def _descriptor(value,spec,profile):
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
        script=Path(_pyc_check.__file__).read_bytes()
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


def admit_closure(raw,independently_accepted_sha256,profile,root,*,candidate_roots):
    """Original registry asset bytes are accepted before decode or physical reads.

    The independently supplied identity must be from the trusted install/release
    owner, not candidate output. It names the descriptor only (Hale H4); the
    archive, manifest M and config/ImageID C are separate rows inside it.
    """
    require(type(raw) is bytes and len(raw)<=MAX_MANIFEST and sha256(raw)==independently_accepted_sha256 and
            profile['inspector'].get('closure_sha256')==independently_accepted_sha256,'inspector_closure_not_accepted')
    spec=profile['inspector']
    # A profile that does not request contained native execution binds no image.
    require(spec.get('execution')=='contained' and spec.get('scope')=='native' and spec.get('python_caches')=='CHECKED_HASH' and
            type(spec.get('closure_id')) is str and type(spec.get('limits')) is dict,'inspector_image_member_unbound')
    startup_argv(profile)
    try:value=decode_json(raw,MAX_MANIFEST)
    except ValueError:raise ReceiptRefusal('inspector_image_member_unbound') from None
    value=_descriptor(value,spec,profile)
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
    record=freeze({'descriptor':value,'diff_ids':['sha256:'+key for key in layers]})
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
            capability.policy_sha256==sha256(canonical_bytes(profile)) and capability.closure_sha256==profile['inspector'].get('closure_sha256'),'inspector_capability_untrusted')
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
    prefix=(str(capability.engine),'--remote=false','--root',str(session/'store'),'--runroot',str(session/'run'),
            '--tmpdir',str(session/'tmp'))
    argv=prefix+('run','--name',name,'--cidfile',str(session/'cid'),'--pull=never','--rm','--read-only',
                 '--read-only-tmpfs=false','--image-volume=ignore','--no-healthcheck','--log-driver=none','--network=none',
                 '--cap-drop=ALL','--security-opt=no-new-privileges','--user',CLOSED_CONFIG['User'],'--workdir=/',
                 '--unsetenv-all','--pids-limit',str(limits['pids']),
                 *cpu_memory_argv(limits['cpu_millicores'],limits['memory_bytes']),'--memory-swap',str(limits['memory_bytes']),
                 '--mount','type=bind,src='+str(session/'request'/'request.json')+',dst='+_pyc_check.REQUEST_PATH+',ro=true,relabel=private',
                 '--entrypoint',STARTUP[0],capability.image_digest,*STARTUP[1:])
    environment=(('CONTAINERS_CONF',record['engine_config']['containers_conf']['path']),
                 ('CONTAINERS_STORAGE_CONF',record['engine_config']['storage_conf']['path']),
                 ('HOME',str(session/'home')),('PATH','/usr/bin:/bin'),('TMPDIR',str(session/'tmp')),
                 ('XDG_RUNTIME_DIR',str(session/'xdg')))
    return InspectorPlan(argv,environment,limits['input_bytes'],limits['output_bytes'],limits['seconds'],
                         limits['memory_bytes'],limits['pids'],False,session,name,prefix,sha256(request_bytes),_ADMITTED)


def _engine(argv,environment,session,timeout,output_limit):
    """One engine operation through the one adapter; returns (exit, stdout)."""
    import secrets
    from .build.engine import run_operation
    stem=session/('op-'+secrets.token_hex(8))
    result=run_operation(argv,session,dict(environment),timeout,Path(str(stem)+'.out'),Path(str(stem)+'.err'),
                         output_limit=output_limit,kill_seconds=KILL_SECONDS)
    return result['exit'],Path(str(stem)+'.out').read_bytes()


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


def run_inspection(capability,plan,request_bytes,*,budget=None):
    """Fresh owned store, load the custody copy, inspect, run, always clean up."""
    from .build.engine import OwnedRoots
    require(isinstance(plan,InspectorPlan) and plan._admission is _ADMITTED and type(request_bytes) is bytes and
            sha256(request_bytes)==plan.request_sha256 and plan.session.parent==capability.root,'inspector_capability_untrusted')
    record=thaw(capability.descriptor);descriptor=record['descriptor']
    environment=dict(plan.environment);session=plan.session
    budget=budget or _Budget(plan.cumulative_seconds)
    remaining=budget.work_timeout
    roots=None
    try:
        try:
            remaining()
            roots=OwnedRoots([session]).create()
            for name in ('store','run','tmp','home','xdg','request'):os.mkdir(session/name,0o700)
            os.chmod(session/'request',0o755)
            _write_owned(session/'request'/'request.json',request_bytes,0o444)
            _,archive=_read_member(capability.root,dict(descriptor['archive'],role='image'),keep=True)
            _write_owned(session/'image.tar',archive,0o444)
            for row in [descriptor['engine'],*descriptor['engine_config'].values()]:_host_file(row,'engine',())
            code,_=_engine(plan.prefix+('load','--input',str(session/'image.tar')),environment,session,remaining(),64*1024)
            require(code==0,'inspector_run_incomplete')
            code,raw=_engine(plan.prefix+('image','inspect','--format','json',capability.image_digest),
                             environment,session,remaining(),1024*1024)
            require(code==0,'inspector_run_incomplete');_loaded(raw,record,capability)
            code,reply=_engine(plan.argv,environment,session,remaining(),plan.output_byte_limit)
            require(code==0,'inspector_run_incomplete')
            _read_member(session/'request',dict(role='image',path='request.json',bytes=len(request_bytes),
                                                 sha256=plan.request_sha256,mode=0o444))
            return reply
        except ReceiptRefusal:raise
        except Exception:raise ReceiptRefusal('inspector_run_incomplete') from None
    finally:
        if roots is not None:_cleanup(plan,environment,session,budget=budget,roots=roots)


def _cleanup(plan,environment,session,*,budget=None,roots=None):
    """Remove the owned container by name and prove it absent; only then the owned roots.

    Unproven absence or a replaced session directory preserves every root for
    accountable recovery. All waits come from the one cleanup reserve.
    """
    budget=budget or _Budget(0)
    def operation(argv):
        timeout=budget.cleanup_timeout()
        if timeout<=0:raise TimeoutError('cleanup reserve spent')
        return _engine(argv,environment,session,timeout,64*1024)
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
    require(absent and roots is not None and _same_root(roots,session),'inspector_run_incomplete')
    try:
        # Rootless store files belong to mapped ids: remove them inside the namespace.
        code,_=operation(plan.prefix+('unshare','rm','-rf','--',str(session/'store'),str(session/'run')))
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
    require(isinstance(capability,InspectorClosure) and capability._admission is _ADMITTED,'trusted_inspector_capability_unavailable')
    return InspectionSession(capability,freeze(profile),_ADMITTED,_Budget(profile['inspector']['limits']['seconds']))


def admit_inspection(verified_input,acceptance):
    profile=thaw(verified_input.metadata)['release_policy']
    require(profile.get('schema')=='cortex.release-policy.v1','inspector_profile_unqualified')
    capability=acceptance.get('inspector_capability')
    require(isinstance(capability,InspectorClosure) and capability._seal is _SEAL and capability._admission is _ADMITTED and
            capability.policy_sha256==sha256(canonical_bytes(profile)) and
            capability.closure_sha256==profile['inspector'].get('closure_sha256'),'trusted_inspector_capability_unavailable')
    value=thaw(verified_input.document);descriptor=thaw(capability.descriptor)['descriptor']
    version='.'.join(value['builder_requirements']['toolchain']['version'].split('.')[:2])
    # Vera ABI rule: the admitted closure's CPython identity, before any candidate reader.
    require(descriptor['python']['version'].rsplit('.',1)[0]==version and
            descriptor['platform']==value['target']['oci_platform'],'inspector_abi_mismatch')
    from .build.policy import verify_native
    try:verify_native(acceptance.get('inspector_host') or {},descriptor['platform'])
    except ValueError:raise ReceiptRefusal('native_inspector_execution_unqualified') from None
    # R3-T2: R8 host eligibility plus a sealed closure are prerequisites only. No
    # admitted R6 qualification evidence exists yet, so execution stays unqualified.
    raise ReceiptRefusal('native_inspector_execution_unqualified')
