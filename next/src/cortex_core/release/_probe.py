"""R6 containment probe (test-only asset), stdlib only.

Byte-identical as /opt/cortex-inspector/inspect.py in the purpose=probe image, run
by the SAME contained argv as the real inspector (only the image digest differs).
It reads its probe name from the one read-only request file and prints ONE closed
JSON observation. It proves only what its row says; it never stands in for the
real inspector. The time probe prints nothing: it is stopped by the run deadline.
"""
import ctypes
import errno
import json
import os
import signal
import socket
import sys
import time

REQUEST_PATH = '/cortex-request.json'
REQUEST_SCHEMA = 'cortex.probe-request.v1'
CANARY = '/opt/cortex-inspector/canary'
TMP_TARGET = '/tmp/cortex-probe'
NETWORK_TARGET = ('1.1.1.1', 443)


def _cgroup(name):
    try:
        with open('/sys/fs/cgroup/' + name) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _events(name, key):
    raw = _cgroup(name) or ''
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key and parts[1].isdigit():
            return int(parts[1])
    return None


def _errno_name(error):
    return errno.errorcode.get(error.errno, str(error.errno))


def network():
    interfaces = sorted(name for _, name in socket.if_nameindex())
    try:
        with socket.create_connection(NETWORK_TARGET, timeout=5):
            outcome = 'CONNECTED'
    except OSError as error:
        outcome = _errno_name(error)
    return {'probe': 'network', 'attempted': '%s:%d' % NETWORK_TARGET, 'interfaces': interfaces, 'errno': outcome}


def _write(path):
    try:
        with open(path, 'w') as handle:
            handle.write('x')
        return 'WRITTEN', True
    except OSError as error:
        return _errno_name(error), False


def rootfs_write():
    outcome, written = _write(CANARY)
    return {'probe': 'rootfs_write', 'target': CANARY, 'errno': outcome, 'written': written}


def tmpfs():
    outcome, written = _write(TMP_TARGET)
    with open('/proc/self/mountinfo') as handle:
        on_tmp = any(line.split()[4] == '/tmp' and ' - tmpfs ' in line for line in handle)
    return {'probe': 'tmpfs', 'target': TMP_TARGET, 'errno': outcome, 'written': written, 'tmpfs_on_tmp': on_tmp}


def pids():
    maximum = _cgroup('pids.max')
    before = _events('pids.events', 'max')
    children, outcome, at_denial = [], 'NO_DENIAL', None
    for _ in range(100000):
        try:
            pid = os.fork()
        except OSError as error:
            outcome = _errno_name(error)
            at_denial = _cgroup('pids.current')
            break
        if pid == 0:
            time.sleep(60)
            os._exit(0)
        children.append(pid)
    after = _events('pids.events', 'max')
    for pid in children:
        os.kill(pid, signal.SIGKILL)
    reaped = all(os.waitpid(pid, 0)[0] == pid for pid in children)
    return {'probe': 'pids', 'errno': outcome, 'pids_max': int(maximum) if maximum and maximum.isdigit() else None,
            'pids_current_at_denial': int(at_denial) if at_denial and at_denial.isdigit() else None,
            'pids_events_max_delta': (after - before) if after is not None and before is not None else None,
            'children': len(children), 'reaped': reaped}


def memory():
    maximum = _cgroup('memory.max')
    before = _events('memory.events', 'oom_kill')
    limit = int(maximum) if maximum and maximum.isdigit() else 0
    attempted = limit * 2 if limit else 1 << 34
    pid = os.fork()
    if pid == 0:
        block = ctypes.create_string_buffer(attempted)  # touched by allocation
        block[attempted - 1] = b'x'
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    after = _events('memory.events', 'oom_kill')
    return {'probe': 'memory', 'memory_max': limit or None, 'attempted_bytes': attempted,
            'child_signal': os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
            'oom_kill_delta': (after - before) if after is not None and before is not None else None}


def time_limit():
    time.sleep(10 ** 6)  # stopped by the run deadline; never prints


def mounts_env():
    with open('/proc/self/mountinfo') as handle:
        mounts = sorted({line.split()[4] for line in handle})
    return {'probe': 'mounts_env', 'mounts': mounts, 'environment': sorted(os.environ)}


PROBES = {'network': network, 'rootfs_write': rootfs_write, 'tmpfs': tmpfs, 'pids': pids, 'memory': memory,
          'time': time_limit, 'mounts_env': mounts_env}


def main():
    with open(REQUEST_PATH, 'rb') as handle:
        request = json.loads(handle.read().decode('utf-8'))
    if type(request) is not dict or set(request) != {'schema', 'probe'} or request['schema'] != REQUEST_SCHEMA \
            or request['probe'] not in PROBES:
        raise SystemExit(2)
    observation = PROBES[request['probe']]()
    sys.stdout.write(json.dumps(observation, sort_keys=True, separators=(',', ':')) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
