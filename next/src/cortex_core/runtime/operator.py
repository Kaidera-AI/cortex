"""Explicit Option-A command dispatcher; existing signed runtime remains separate."""
import json
import hashlib
import os
import re
import sys
import time
from uuid import UUID

from .bootstrap_config import read_config_file
from .custody import open_custody
from .custody_fs import anonymous_pipe
from .signed_member import Refusal

_FD = re.compile(r'(?:0|[1-9][0-9]{0,9})\Z')
_REASON = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


def _arguments(argv):
    if type(argv) is not list or not argv or any(type(s) is not str for s in argv):
        raise Refusal('operator_arguments')
    if argv[:2] == ['config','client']:
        command='client';index=2
    elif argv[0] in ('serve','migrate','seed','status'):
        command=argv[0];index=1
    else:
        raise Refusal('operator_arguments')
    allowed={'--config'}
    if command!='status':allowed|={'--unlock-fd','--key-id'}
    if command=='client':allowed|={'--projection-fd','--project'}
    if command=='seed':allowed|={'--operation-key'}
    values={}
    while index<len(argv):
        name=argv[index]
        if name not in allowed or name in values or index+1>=len(argv):
            raise Refusal('operator_arguments')
        values[name]=argv[index+1];index+=2
    required=allowed-{'--project'}
    if not required<=values.keys() or any(not value for value in values.values()):
        raise Refusal('operator_arguments')
    for key in ('--unlock-fd','--projection-fd'):
        if key in values:
            if _FD.fullmatch(values[key]) is None:
                raise Refusal('operator_projection' if key=='--projection-fd' else 'operator_arguments')
            values[key]=int(values[key])
    return command,values


def _client(values):
    output=values['--projection-fd']
    if output in (0,1,2) or not anonymous_pipe(output,writing=True):
        raise Refusal('operator_projection')
    # This explicitly transferred output descriptor is owned by this command;
    # closing it on every exit supplies EOF. Unlock is borrowed and duplicated.
    try:
        config=read_config_file(values['--config'],mode='client')
        selected=values.get('--project')
        if selected is None:
            selected=next(name for name,identity in config.project_names.items() if identity==config.project_id)
        if selected not in config.project_names:
            raise Refusal('config_projects')
        host=config.publish['host'];host='['+host+']' if ':' in host else host
        with open_custody(config,unlock_fd=values['--unlock-fd'],key_id=values['--key-id']) as vault:
            credential=vault.resolve('client').decode('ascii','strict')
            fields=['cortex.client.v1',f'http://{host}:{config.publish["port"]}',
                config.installation_id,config.project_names[selected],selected,credential,
                str(min(config.limits['request_seconds'],10))]
            if any(any(c in field for c in ('\x00','\r','\n')) for field in fields):
                raise Refusal('operator_projection')
            wire=b''.join(field.encode('utf-8')+b'\x00' for field in fields)
            if len(wire)>os.fpathconf(output,'PC_PIPE_BUF'):
                raise Refusal('operator_projection')
            os.set_blocking(output,False)
            try:
                if os.write(output,wire)!=len(wire):
                    raise Refusal('operator_projection')
            except OSError:
                raise Refusal('operator_projection') from None
            finally:
                del credential,fields,wire
    finally:
        os.close(output)


def dispatch_operator_command(argv):
    command,values=_arguments(argv)
    if command=='client':
        _client(values)
        return 0
    if command=='serve':
        import asyncio
        from .lifecycle import serve_runtime
        config=read_config_file(values['--config'],mode='serve')
        return asyncio.run(serve_runtime(config,unlock_fd=values['--unlock-fd'],key_id=values['--key-id']))
    if command=='migrate':
        from .resources import open_migration_resources
        config=read_config_file(values['--config'],mode='migrate')
        deadline=time.monotonic()+config.limits['request_seconds']
        with open_custody(config,unlock_fd=values['--unlock-fd'],key_id=values['--key-id']) as vault:
            with open_migration_resources(config,custody=vault,deadline=deadline) as resources:
                applied=resources.migrate()
        # Success is emitted only after the actual engine and owned cleanup.
        print(json.dumps({'schema':'cortex.operator.v1','ok':True,'operation':'migrate',
            'installation_id':config.installation_id,'applied':applied},separators=(',',':')))
        return 0
    if command=='seed':
        from .resources import open_seed_resources,remaining_seconds
        from cortex_core.release.codec import canonical_bytes
        config=read_config_file(values['--config'],mode='seed')
        key=values['--operation-key']
        try:
            if (not 1<=len(key)<=256 or len(key.encode('utf-8','strict'))>1024
                    or any(ord(char)<32 or ord(char)==127 for char in key)):
                raise ValueError
        except (ValueError,UnicodeError):raise Refusal('seed_operation_key') from None
        deadline=time.monotonic()+config.limits['request_seconds']
        try:
            from cortex_core.schema.seed import seed_runtime_bindings
        except ImportError:raise Refusal('seed_unavailable') from None
        with open_custody(config,unlock_fd=values['--unlock-fd'],key_id=values['--key-id']) as vault:
            with open_seed_resources(config,custody=vault,deadline=deadline):
                result=seed_runtime_bindings(config,key,deadline)
            # A callable's return alone is not durable seed acknowledgement.
            # Reload only after all owned SQL connections have closed.
            with vault.seed_record() as record:
                material=record.load()
                keys={'installation_id','tenant_id','project_id','principal_id','credential_id',
                    'operation_key','credential_sha256','intent_sha256'}
                if (material is None or material.state!='committed' or type(result) is not dict
                        or set(result)!=keys or material.receipt!=result):
                    raise Refusal('seed_receipt')
                intent=material.intent
                expected={name:intent.get(name) for name in keys-{'intent_sha256'}}
                expected['intent_sha256']=hashlib.sha256(canonical_bytes(intent)).hexdigest()
                if (result!=expected or result['operation_key']!=key
                        or result['installation_id']!=config.installation_id
                        or result['tenant_id']!=config.tenant_id or result['project_id']!=config.project_id
                        or result['credential_sha256']!=hashlib.sha256(material.credential).hexdigest()):
                    raise Refusal('seed_receipt')
                try:
                    for name in ('principal_id','credential_id'):
                        if type(result[name]) is not str or str(UUID(result[name]))!=result[name] or UUID(result[name]).int==0:
                            raise ValueError
                except (ValueError,TypeError,AttributeError):raise Refusal('seed_receipt') from None
                remaining_seconds(deadline)
        print(json.dumps({'schema':'cortex.operator.v1','ok':True,'operation':'seed',
            'installation_id':config.installation_id,'receipt':result},separators=(',',':')))
        return 0
    # Partial dependency publication: no placeholder can admit another effect.
    raise Refusal('operator_unbound')


def main(argv=None):
    try:
        return dispatch_operator_command(sys.argv[1:] if argv is None else argv)
    except Refusal as error:
        reason=error.reason if type(error.reason) is str and _REASON.fullmatch(error.reason) else 'operator_unavailable'
    except KeyboardInterrupt:
        reason='operator_interrupted'
    except Exception:
        reason='operator_unavailable'
    print(json.dumps({'schema':'cortex.operator.v1','ok':False,
        'error':{'code':'core_refusal','reason':reason}},separators=(',',':')),file=sys.stderr)
    return 130 if reason=='operator_interrupted' else 2


if __name__=='__main__':raise SystemExit(main())
