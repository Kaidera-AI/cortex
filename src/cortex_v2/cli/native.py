"""Finite Linux CM-2 commands; existing human identity commands retain their API."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import datetime
import json
import os
from pathlib import Path
import platform
import re
import select
import signal
import stat
import sys
import time

from . import keys
from ..clients import linux_provisioning as host
from ..clients import native_prerequisite as custody
from ..clients.provisioning import validate_request


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise custody.PrerequisiteRefusal('cortex_descriptor_invalid')


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise custody.PrerequisiteRefusal('cortex_descriptor_invalid')
        setattr(namespace, self.dest, values)


class _Discard:
    """Discard diagnostics without retaining private strings in a buffer."""
    def write(self, value): return len(value)
    def flush(self): pass


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise custody.PrerequisiteRefusal('cortex_health_unavailable')


@contextmanager
def _wall_deadline(deadline):
    """Bound even a blocked pipe or accidental blocking operation on Linux."""
    prior = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(signum, frame):
        raise custody.PrerequisiteRefusal('cortex_health_unavailable')
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline-started))
        yield
        _check_deadline(deadline)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior)
        if timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, timer[0]-(time.monotonic()-started)), timer[1])


def _absolute(value):
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or any(ord(c)<32 for c in value)):
        raise custody.PrerequisiteRefusal('cortex_descriptor_invalid')
    return path


def _read_owner(fd, deadline):
    duplicate = None
    try:
        if type(fd) is not int or fd < 3: raise ValueError
        duplicate = os.dup(fd)
        initial = os.fstat(duplicate)
        if initial.st_uid != os.getuid(): raise ValueError
        regular = stat.S_ISREG(initial.st_mode)
        if regular:
            custody._private_file(initial)
            if initial.st_size not in (43,44): raise ValueError
        elif not stat.S_ISFIFO(initial.st_mode): raise ValueError
        raw = bytearray()
        while True:
            _check_deadline(deadline)
            ready,_,_ = select.select([duplicate],[],[],max(0,deadline-time.monotonic()))
            if not ready: raise ValueError
            part = os.read(duplicate,45-len(raw))
            _check_deadline(deadline)
            if not part: break
            raw.extend(part)
            if len(raw)>44: raise ValueError
        if regular and ((custody._file_identity(os.fstat(duplicate)),os.fstat(duplicate).st_ctime_ns)
                        != (custody._file_identity(initial),initial.st_ctime_ns)): raise ValueError
        value=bytes(raw)
        if value.endswith(b'\n'): value=value[:-1]
        if re.fullmatch(rb'[A-Za-z0-9_-]{43}',value) is None: raise ValueError
        return value
    except custody.PrerequisiteRefusal:
        raise
    except Exception:
        raise custody.PrerequisiteRefusal('cortex_provisioning_owner_required') from None
    finally:
        if duplicate is not None: os.close(duplicate)


def _public_line(value, limit):
    try:
        line=json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))+'\n'
        if len(line.encode('utf-8'))>limit: raise ValueError
        return line
    except Exception:
        raise custody.PrerequisiteRefusal('cortex_descriptor_invalid') from None


def _fields(value, fields):
    if not isinstance(value,dict) or set(value)!=set(fields): raise ValueError


def _validate_proof(value, nonce):
    predicates={'source_payload_matches','migration_checksums_match','required_rls_enabled_and_forced',
        'app_role_non_superuser_without_bypassrls','app_role_not_migrator_or_table_owner',
        'database_instance_matches','signed_helper_verified'}
    _fields(value,predicates|{'schema','nonce','descriptor_sha256','installation_id','release_manifest_sha256',
        'target','engine','api_binding','images','selinux','helper_sha256','project_binding'})
    if (value['schema']!='cortex.prerequisite-proof.v2' or value['nonce']!=nonce
            or value['target']!='linux-x86_64' or value['selinux']!='Enforcing'
            or any(value[k] is not True for k in predicates)): raise ValueError
    custody.canonical_uuid(value['installation_id'])
    for key in ('descriptor_sha256','release_manifest_sha256','helper_sha256'):
        if not isinstance(value[key],str) or custody.HEX.fullmatch(value[key]) is None: raise ValueError
    engine=value['engine']
    _fields(engine,{'client_version','server_version','rootless','os','architecture','provider','mode','connection_name'})
    if (not isinstance(engine['client_version'],str) or re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)',engine['client_version']) is None
            or engine['server_version'] is not None or engine['connection_name'] is not None
            or engine['rootless'] is not True or engine['os']!='linux' or engine['architecture']!='amd64'
            or engine['mode']!='local' or engine['provider']!='cortex-native-lifecycle'): raise ValueError
    _fields(value['images'],custody.ROLES)
    for image in value['images'].values():
        if not isinstance(image,str) or re.fullmatch(r'sha256:[0-9a-f]{64}',image) is None: raise ValueError
    binding=value['api_binding']
    _fields(binding,{'loopback_port','installation_label_matches','api_image_id','exact_owned_network','network_internal'})
    if (type(binding['loopback_port']) is not int or not 1024<=binding['loopback_port']<=65535
            or binding['loopback_port'] in (8501,5499,5500) or binding['api_image_id']!=value['images']['api']
            or any(binding[k] is not True for k in ('installation_label_matches','exact_owned_network','network_internal'))): raise ValueError
    project=value['project_binding'];_fields(project,{'scope_id','primary_alias','primary_root'})
    custody.canonical_uuid(project['scope_id']);_absolute(project['primary_root'])
    if not isinstance(project['primary_alias'],str) or custody.IDENTIFIER.fullmatch(project['primary_alias']) is None: raise ValueError


def _validate_receipt(value, connection, descriptor):
    _fields(value,{'status','operation_id','installation_id','principal_id','actor_id','scope_id',
        'connection_file','descriptor_file','expires_at'})
    if (value['status']!='READY' or value['connection_file']!=str(connection)
            or value['descriptor_file']!=str(descriptor)): raise ValueError
    for key in ('operation_id','installation_id','principal_id','actor_id','scope_id'):custody.canonical_uuid(value[key])
    expires=datetime.datetime.fromisoformat(value['expires_at'])
    if expires.tzinfo is None or expires<=datetime.datetime.now(datetime.timezone.utc): raise ValueError


def _arguments(argv):
    parser=_Parser(prog='cortex',allow_abbrev=False)
    commands=parser.add_subparsers(dest='command',required=True,parser_class=_Parser)
    proof=commands.add_parser('prerequisite-proof',allow_abbrev=False)
    for flag in ('descriptor','nonce'):proof.add_argument('--'+flag,required=True,action=_Once)
    provision=commands.add_parser('provision-console',allow_abbrev=False)
    for flag in ('runtime-root','request','kos-policy','owner-fd','idempotency-key','connection','descriptor'):
        provision.add_argument('--'+flag,required=True,action=_Once)
    return parser.parse_args(argv)


def main(argv=None, *, out=None, err=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    out=sys.stdout if out is None else out;err=sys.stderr if err is None else err
    if not argv or not any(a in ('provision-console','prerequisite-proof') for a in argv):
        return keys.human_main(argv,out=out,err=err)
    owner=None;deadline=time.monotonic()+30
    try:
        with _wall_deadline(deadline),redirect_stdout(_Discard()),redirect_stderr(_Discard()):
            args=_arguments(argv)
            if platform.system()!='Linux' or os.getuid()==0:
                raise custody.PrerequisiteRefusal('cortex_release_unsupported')
            descriptor=_absolute(args.descriptor)
            if args.command=='prerequisite-proof':
                if re.fullmatch(r'[0-9a-f]{32}',args.nonce) is None:raise ValueError
                value=host.read_linux_prerequisite_proof(descriptor,args.nonce,deadline=deadline)
                _validate_proof(value,args.nonce)
            else:
                root=_absolute(args.runtime_root);request=_absolute(args.request);policy_path=_absolute(args.kos_policy)
                connection=_absolute(args.connection)
                if len({descriptor,connection,request,policy_path})!=4:raise ValueError
                custody.physical_path(root)
                for path in (descriptor,connection):custody._parents(path)
                body=custody.read_private_json(request)
                policy=custody.read_private_json(policy_path)
                incoming={'mode':'create-project','host_os':'linux','create_project':body,
                    'operator_explicit':True,'manager_explicit':True,'idempotency_key':args.idempotency_key}
                validate_request(incoming);_check_deadline(deadline)
                owner=_read_owner(int(args.owner_fd),deadline)
                value=host.provision_linux(root,incoming,owner_token=owner,kos_policy=policy,
                    connection_file=connection,descriptor_file=descriptor,deadline=deadline)
                _validate_receipt(value,connection,descriptor)
            line=_public_line(value,65536);_check_deadline(deadline)
            out.write(line);out.flush();_check_deadline(deadline)
        return 0
    except BaseException as error:
        if isinstance(error,(KeyboardInterrupt,GeneratorExit)):return 4
        code=error.code if isinstance(error,custody.PrerequisiteRefusal) and error.code in host.REFUSALS else 'cortex_descriptor_invalid'
        status=error.http_status if isinstance(error,custody.PrerequisiteRefusal) else None
        if type(status) is not int or not 100<=status<=599:status=None
        refusal=custody.PrerequisiteRefusal(code,http_status=status).public()
        try:err.write(_public_line(refusal,4096));err.flush()
        except Exception:return 4
        return 2
    finally:
        owner=None


if __name__=='__main__':
    raise SystemExit(main())
