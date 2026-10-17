"""One public configured health read; never unlock custody or open SQL."""
import sys
import time

import httpx

from cortex_core.cli.status import BUDGET_SECONDS,MAX_BODY_BYTES,check_deadline,emit,endpoint_url
from .bootstrap_config import BootstrapConfig
from .contracts import decode
from .resources import _driver_deadline
from .signed_member import Refusal


def _health(raw,http_status,installation_id):
    value=decode(raw,limit=MAX_BODY_BYTES)
    if (type(value) is not dict or set(value)!={'component','status','readiness','installation_id'}
            or any(type(item) is not str for item in value.values())
            or value['component']!='cortex' or value['installation_id']!=installation_id):
        raise ValueError()
    states={'ready':(200,'ok',0),'enrollment_required':(200,'ok',1),
            'unavailable':(503,'unavailable',2)}
    expected=states.get(value['readiness'])
    if expected is None or (http_status,value['status'])!=expected[:2]:raise ValueError()
    return value,expected[2]


def read_status(config):
    health,error,code=None,None,2
    try:
        if type(config) is not BootstrapConfig or config.mode!='status':raise ValueError()
        host=config.publish['host'];host='['+host+']' if ':' in host else host
        url=endpoint_url(f'http://{host}:{config.publish["port"]}')
        deadline=time.monotonic()+BUDGET_SECONDS
        # This operator is a single main-thread owner. The same absolute timer
        # bounds synchronous DNS, callbacks and body iteration, not only sockets.
        with _driver_deadline(deadline):
            with httpx.Client(trust_env=False,follow_redirects=False,verify=True,
                              timeout=BUDGET_SECONDS) as client:
                with client.stream('GET',url,headers={'Accept':'application/json','Accept-Encoding':'identity'}) as response:
                    check_deadline(deadline)
                    if (response.status_code not in {200,503}
                            or response.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json'
                            or response.headers.get('content-encoding','identity').lower()!='identity'):
                        raise ValueError()
                    body=bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        check_deadline(deadline)
                        if len(body)+len(chunk)>MAX_BODY_BYTES:raise ValueError()
                        body.extend(chunk)
                    health,code=_health(bytes(body),response.status_code,config.installation_id)
                    check_deadline(deadline)
            check_deadline(deadline)
    except (TimeoutError,httpx.TimeoutException):
        health,error,code=None,'timeout',2
    except Refusal as refusal:
        health,error,code=None,('timeout' if refusal.reason=='resources_deadline' else 'invalid_response'),2
    except (ValueError,UnicodeError):
        health,error,code=None,'invalid_response',2
    except Exception:
        health,error,code=None,'connection_unavailable',2
    emit(sys.stdout,True,health,error)
    return code
