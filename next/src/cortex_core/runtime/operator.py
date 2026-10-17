"""Explicit Option-A command dispatcher; existing signed runtime remains separate."""
import json
import os
import re
import sys

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
