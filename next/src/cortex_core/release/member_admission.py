"""Sole read-only R01 backend to the existing RC2 original-member port.

Fixed owner composition is UNBOUND before arguments/imports/physical reads.
Private context/transport callables are trusted installed-owner dependencies,
never client parameters or an authority binder. Source test composition proves
neither protected installation nor current native/image/effect authority.
Images retain their separate future stream gate; this snapshot stays <=262144.
"""
from dataclasses import dataclass
import re
import time
from types import SimpleNamespace

from cortex_core.runtime.signed_member import Refusal, MISSING_UNIT, MAX_MEMBER_BYTES, sha256

_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NAME = re.compile(r'members/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)*\Z')


@dataclass(frozen=True)
class _StartupContext:
    """Independent bootstrap DATA, not a seal or protection/effect receipt.

    The fixed provider is unbound. Trusted bootstrap must admit the actual
    interpreter/import/native closure BEFORE this Python code executes; checking
    its own candidate hash cannot establish that provenance. Host proof is held.
    """
    bootstrap_id: str
    executable: str
    entrypoint: str
    python_version: tuple
    import_roots: tuple
    cache_prefix: str
    files: tuple
    intrinsics: tuple
    path_hooks: tuple
    owner_binding: tuple
    current: object
    originals: object
    module_aliases: tuple=()


def _installed_startup():
    # No env/config/argv/root/callback binder, repair or fixture fallback.
    raise Refusal('required_binding_gap', unit=MISSING_UNIT)


def _startup_identity(startup):
    if type(startup) is not _StartupContext:
        raise Refusal('required_binding_gap', unit=MISSING_UNIT)
    from .codec import relative_path
    from .inspector import MAX_MEMBERS
    def absolute(path):
        return type(path) is str and path.startswith('/') and relative_path(path[1:])==path[1:]
    try:
        valid=(type(startup.bootstrap_id) is str and 0<len(startup.bootstrap_id)<=128
               and all(absolute(p) for p in (startup.executable,startup.entrypoint,startup.cache_prefix))
               and type(startup.python_version) is tuple and len(startup.python_version)==3
               and all(type(n) is int and 0<=n<10000 for n in startup.python_version)
               and type(startup.import_roots) is tuple and 0<len(startup.import_roots)<=32
               and all(absolute(p) for p in startup.import_roots)
               and len(set(startup.import_roots))==len(startup.import_roots)
               and type(startup.files) is tuple and 0<len(startup.files)<=MAX_MEMBERS
               and type(startup.intrinsics) is tuple and 0<len(startup.intrinsics)<=512
               and all(type(n) is str and 0<len(n)<=256 for n in startup.intrinsics)
               and startup.intrinsics==tuple(sorted(set(startup.intrinsics)))
               and type(startup.path_hooks) is tuple and len(startup.path_hooks)==2
               and type(startup.owner_binding) is tuple and len(startup.owner_binding)==3
               and type(startup.owner_binding[0]) is str
               and re.fullmatch(r'[0-9a-f]{64}',startup.owner_binding[0]) is not None
               and all(type(n) is int and 0<n<2**63 for n in startup.owner_binding[1:])
               and callable(startup.current) and callable(startup.originals))
        if not valid:raise ValueError('missing bootstrap DATA')
        roles=set();paths=[]
        for row in startup.files:
            if not (type(row) is tuple and len(row)==5 and type(row[0]) is str
                    and row[0] in {'interpreter','source','stdlib','loader','crypto','data'}
                    and absolute(row[1]) and type(row[2]) is int and 0<=row[2]<=256*1024**2
                    and type(row[3]) is str and re.fullmatch(r'[0-9a-f]{64}',row[3]) is not None
                    and type(row[4]) is int and 0<=row[4]<=0o777):
                raise ValueError('invalid original table')
            roles.add(row[0]);paths.append(row[1])
        if not (type(startup.module_aliases) is tuple and len(startup.module_aliases)<=512):
            raise ValueError('invalid native aliases')
        native_paths={r[1] for r in startup.files if r[0] in {'loader','crypto'}}
        alias_names=[]
        for row in startup.module_aliases:
            if not (type(row) is tuple and len(row)==3 and type(row[0]) is str and 0<len(row[0])<=256
                    and row[1] is not None and type(row[2]) is str and row[2] in native_paths):
                raise ValueError('unbound native alias')
            alias_names.append(row[0])
        if alias_names!=sorted(set(alias_names)):raise ValueError('duplicate native aliases')
        if (paths!=sorted(set(paths)) or not {'interpreter','source','stdlib','loader','crypto'}<=roles
                or not any(r[0]=='interpreter' and r[1]==startup.executable for r in startup.files)
                or not any(r[0]=='source' and r[1]==startup.entrypoint for r in startup.files)):
            raise ValueError('incomplete original table')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise Refusal('required_binding_gap', unit=MISSING_UNIT) from None
    return (startup.bootstrap_id,startup.executable,startup.entrypoint,startup.python_version,
            startup.import_roots,startup.cache_prefix,startup.files,startup.intrinsics,startup.owner_binding,
            tuple((name,id(value),path) for name,value,path in startup.module_aliases))


def _startup_observation():
    """Actual trusted-runtime observations; never a public caller parameter."""
    import importlib.machinery as machinery
    import os
    import sys
    from types import ModuleType
    modules=[]
    for name,module in tuple(sys.modules.items()):
        if module is None:continue  # blocked optional imports execute no code
        if type(module) is not ModuleType:
            modules.append((name,None,object,()));continue
        values=vars(module);spec=values.get('__spec__')
        if name=='__main__' and spec is None:
            modules.append((name,values.get('__file__'),type(None),()));continue
        if type(spec) is not machinery.ModuleSpec:
            modules.append((name,None,object,()));continue
        loader=spec.loader
        kind=loader if loader is machinery.BuiltinImporter or loader is machinery.FrozenImporter else type(loader)
        modules.append((name,spec.origin,kind,tuple(spec.submodule_search_locations or ())))
    importers=[]
    for path,finder in tuple(sys.path_importer_cache.items()):
        if finder is not None:
            importers.append((path,type(finder),tuple(finder._loaders) if type(finder) is machinery.FileFinder else ()))
    return SimpleNamespace(executable=sys.executable,entrypoint=sys.argv[0] if sys.argv else None,
        python_version=tuple(sys.version_info[:3]),import_roots=tuple(sys.path),cache_prefix=sys.pycache_prefix,
        flags=(sys.flags.isolated,sys.flags.ignore_environment,sys.flags.no_site,sys.flags.no_user_site,
               sys.flags.safe_path,sys.flags.optimize,sys.dont_write_bytecode),environment=tuple(os.environ),
        meta_path=tuple(sys.meta_path),path_hooks=tuple(sys.path_hooks),importers=tuple(importers),modules=tuple(modules),
        module_objects=tuple(sys.modules.items()))


def _admit_startup(startup):
    """Source consistency under A's already trusted bootstrap, never B proof."""
    _startup_identity(startup)
    import importlib.machinery as machinery
    from pathlib import Path
    import zipimport
    from . import inspector,trust,aggregate
    from .custody import RootReader
    require=trust._require
    started=time.monotonic()
    def observe():
        actual=_startup_observation()
        require(actual.flags==(1,1,1,1,True,0,True),'member_authority_unavailable')
        require(actual.executable==startup.executable and actual.entrypoint==startup.entrypoint
                and actual.python_version==startup.python_version,'member_authority_unavailable')
        require(actual.import_roots==startup.import_roots and actual.cache_prefix==startup.cache_prefix,
                'member_authority_unavailable')
        require(set(actual.environment)<={'LC_CTYPE','__CF_USER_TEXT_ENCODING'},'member_authority_unavailable')
        require(len(actual.meta_path)==3 and all(a is b for a,b in zip(actual.meta_path,
                    (machinery.BuiltinImporter,machinery.FrozenImporter,machinery.PathFinder))),
                'member_authority_unavailable')
        require(len(actual.path_hooks)==2 and all(a is b for a,b in zip(actual.path_hooks,startup.path_hooks))
                and actual.path_hooks[0] is zipimport.zipimporter,'member_authority_unavailable')
        return actual
    actual=observe()
    owner=startup.current()
    policy=trust._owner_policy(owner);limits=aggregate._limits(policy)
    require((owner.policy_sha256,owner.generation,owner.fence)==startup.owner_binding,'aggregate_replay')
    require(len(startup.files)<=limits['member_count']
            and sum(row[2] for row in startup.files)<=limits['aggregate_bytes'],'raw_limit')
    reader=RootReader('/',256*1024**2);reader._aggregate_deadline=started+limits['read_seconds']
    try:
        reader._aggregate_time()
        require(reader.inventory(startup.cache_prefix[1:])==[],'member_authority_unavailable')
        stamp=[]
        for role,path,length,digest,mode in startup.files:
            reader._aggregate_time()
            try:
                row=inspector._read_member(Path('/'),dict(role=role,path=path[1:],bytes=length,sha256=digest,mode=mode))
            except (OSError,ValueError):
                raise Refusal('member_custody_changed') from None
            # Keep the owner-selected absolute identity in the private stamp.
            stamp.append((row[0],path,*row[2:]))
            reader._aggregate_time()
        actual=observe()  # include fixed dependencies loaded while measuring
        admitted={row[1] for row in startup.files}
        expected_loaders=tuple((suffix,loader) for loader,suffixes in
            ((machinery.ExtensionFileLoader,machinery.EXTENSION_SUFFIXES),
             (machinery.SourceFileLoader,machinery.SOURCE_SUFFIXES),
             (machinery.SourcelessFileLoader,machinery.BYTECODE_SUFFIXES)) for suffix in suffixes)
        def under(path):
            return type(path) is str and any(path==root or path.startswith(root+'/') for root in startup.import_roots)
        for path,kind,loaders in actual.importers:
            require(under(path) and kind is machinery.FileFinder and loaders==expected_loaders,'member_authority_unavailable')
        aliases={name:(value,path) for name,value,path in startup.module_aliases}
        objects=dict(getattr(actual,'module_objects',()))
        loaded_native={origin for _,origin,kind,_ in actual.modules if kind is machinery.ExtensionFileLoader}
        for name,origin,kind,locations in actual.modules:
            require(type(name) is str and not any(part in {'tests','r01u3_support','unittest','pytest'} or part.startswith('test_')
                    for part in name.split('.')),'member_authority_unavailable')
            if name in aliases:
                value,path=aliases[name]
                require(origin is None and not locations and objects.get(name) is value and path in loaded_native,
                        'member_authority_unavailable')
                continue  # exact bootstrap-held alias; no new executable import
            if origin in ('built-in','frozen'):
                require(name in startup.intrinsics and kind is (machinery.BuiltinImporter if origin=='built-in' else machinery.FrozenImporter),
                        'member_authority_unavailable')
            elif origin is None:
                require(kind is machinery.NamespaceLoader and bool(locations)
                        and all(under(path) for path in locations),'member_authority_unavailable')
            else:
                require(type(origin) is str and origin in admitted and
                        ((name=='__main__' and origin==startup.entrypoint and kind is type(None))
                         or kind in (machinery.SourceFileLoader,machinery.ExtensionFileLoader)),
                        'member_authority_unavailable')
            require(all(under(path) for path in locations),'member_authority_unavailable')
        require(reader.inventory(startup.cache_prefix[1:])==[],'member_authority_unavailable')
        reader._aggregate_time()
    finally:
        reader.close()
    reader._aggregate_time()  # the same startup deadline includes actual cleanup
    return owner,tuple(stamp)


def _owner_originals():
    # The fixed independent owner delivers startup DATA; no public binder exists.
    startup=_installed_startup();identity=_startup_identity(startup)
    _,stamp=_admit_startup(startup)
    def current():
        fresh=_installed_startup()
        if _startup_identity(fresh)!=identity:raise Refusal('aggregate_replay')
        owner,observed=_admit_startup(fresh)
        if observed!=stamp:raise Refusal('member_custody_changed')
        return owner
    return current,startup.originals


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
            # The final owner reload follows C's EOF work. Reobserve the same
            # closed inventory before terminal held/name/deadline verification.
            reader._aggregate_inventory_finish()
            # C measured the whole original length/digest against this signed
            # row; its EOF recheck holds every original. Bytes are immutable.
            original = bodies[member_name]
        except Refusal:
            raise
        except Exception:
            raise Refusal('member_authority_unavailable') from None
        finally:
            if reader is not None:
                try: reader.close()
                except Exception: raise Refusal('member_authority_unavailable') from None
        # The original shared deadline also covers actual descriptor cleanup.
        budget()
        return original, authenticated

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
