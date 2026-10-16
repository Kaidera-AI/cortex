"""Finite X11-c adapter behind P1's existing snapshot/dispatch/measure seam.

PRIVATE SOURCE COMPOSITION ONLY. The sole installed engine/held-X2 supplier is
UNBOUND. A plan/observation/command reply is DATA, never an owner grant. No public
binder, environment switch, launcher, new resource kind or second history exists.
X11-d must join the held measurement to X1's non-SQL persistence before installation.

The engine seam owns native collection and bounded nonblocking child IO. It must
hold the independently enforced distinct-owner/backend exclusion, originals,
executable/import/config descriptors and exact lifetimes until _open exits; run
must use the supplied absolute deadline for every pipe/child/descendant/reap.
Missing native collectors cannot be replaced by these source predicates.
"""
from contextlib import contextmanager
from pathlib import PurePosixPath
import re

from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from cortex_core.release.build.engine import cpu_memory_argv
from cortex_core.runtime.contracts import load_contracts

from . import owner
from .contracts import absolute_path

__all__ = []
_LIMIT = 65536
_HEX = re.compile(r'^[0-9a-f]{64}$')
_NAME = re.compile(r'^[a-z][a-z0-9-]{0,62}$')
_ACTIONS = {'image': {'load', 'remove'}, 'volume': {'create', 'remove'},
            'network': {'create', 'remove', 'connect', 'disconnect'},
            'container': {'create', 'start', 'signal', 'stop', 'remove', 'helper'}}
_ENV = frozenset(('HOME', 'XDG_RUNTIME_DIR', 'CONTAINERS_CONF', 'CONTAINERS_STORAGE_CONF'))


def _need(condition, detail='backend_unavailable'):
    owner._require(condition, detail)


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value < 2**63


def _id(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _digest(value):
    return type(value) is str and value.startswith('sha256:') and _id(value[7:])


def _path(value):
    try:
        absolute_path(value)
        return '\x00' not in value and ',' not in value and ':' not in value
    except ValueError:
        return False


def _copy(value):
    # Bound internal facts using the existing codec; no second wire parser.
    _need(len(canonical_bytes(thaw(value))) <= 262144)
    return freeze(thaw(value))


def _binding(b):
    """Continuity/shape of the fixed collector's held facts, not provenance."""
    _need(type(b) is dict)
    for key in ('executable', 'root', 'runroot', 'tmpdir', 'home', 'core_executable'):
        _need(_path(b.get(key)))
    _need(PurePosixPath(b['executable']).name == 'podman', 'custody_changed')
    uid = b.get('owner_uid'); peer = b.get('peer_uid')
    _need(_integer(uid) and _integer(peer) and uid != peer, 'peer_denied')
    _need(b.get('backend_uid') == uid and b.get('owner_groups')
          and b.get('peer_groups') and not set(b['owner_groups']) & set(b['peer_groups']), 'peer_denied')
    _need(type(b.get('store_identity')) is list and len(b['store_identity']) == 3
          and all(_integer(n) for n in b['store_identity'])
          and b['store_identity'][2] == uid, 'custody_changed')
    for key in ('boot', 'namespace', 'clock_epoch', 'source', 'version'):
        _need(type(b.get(key)) is str and 0 < len(b[key]) <= 128)
    _need(b.get('target_arch') in ('arm64', 'amd64'))
    exclusion = b.get('exclusion')
    _need(type(exclusion) is dict and set(exclusion) == {'holder', 'generation', 'participants', 'bypass_uids'}
          and type(exclusion['holder']) is str and exclusion['holder']
          and _integer(exclusion['generation']) and exclusion['participants'] == [uid]
          and exclusion['bypass_uids'] == [], 'atomicity_unavailable')
    _need(_lifetime(b.get('owner_lifetime'), b), 'peer_denied')
    _need(type(b.get('authority')) is list and len(b['authority']) == 4
          and all(type(x) is str and x for x in b['authority'][:3])
          and _integer(b['authority'][3], 0), 'owner_unavailable')
    closure = b.get('closure')
    _need(type(closure) is list and 0 < len(closure) <= 256, 'custody_changed')
    _need(all(type(x) is dict and _path(x.get('path')) and _id(x.get('sha256'))
          and _integer(x.get('dev')) and _integer(x.get('ino'))
          and x.get('uid') in (0, uid) and x.get('writable_by') == [] for x in closure), 'custody_changed')
    _need(len({x['path'] for x in closure}) == len(closure)
          and b['executable'] in [x['path'] for x in closure], 'custody_changed')
    env = b.get('environment')
    _need(type(env) is dict and set(env) == _ENV and all(_path(x) for x in env.values())
          and env['HOME'] == b['home'] and env['XDG_RUNTIME_DIR'] == b['runroot'], 'custody_changed')
    _need({env['CONTAINERS_CONF'], env['CONTAINERS_STORAGE_CONF']} <= {x['path'] for x in closure}, 'custody_changed')


def _lifetime(value, binding):
    return (type(value) is dict and set(value) == {'boot', 'pid', 'start', 'uid'}
            and value['boot'] == binding['boot'] and value['uid'] == binding['owner_uid']
            and _integer(value['pid']) and _integer(value['start']))


def _resource(kind, value, binding, *, selected=True):
    _need(type(value) is dict and type(value.get('consumers')) is list
          and len(set(value['consumers'])) == len(value['consumers'])
          and all(_id(c) for c in value['consumers']), 'resource_changed')
    _need(type(value.get('transaction')) is str and value['transaction'], 'resource_changed')
    if kind == 'volume':
        m = value.get('mountpoint')
        _need(_NAME.fullmatch(value.get('name', '')) and type(value.get('created_at')) is str
              and value['created_at'] and value.get('driver') == 'local' and value.get('options') == {}
              and type(m) is dict and set(m) == {'path', 'dev', 'ino', 'uid'}
              and _path(m['path']) and PurePosixPath(m['path']).is_relative_to(binding['root'])
              and _integer(m['dev']) and _integer(m['ino']) and m['uid'] == binding['owner_uid'], 'resource_changed')
        return
    _need(_id(value.get('id')), 'resource_changed')
    if kind == 'image':
        _need(_digest(value.get('manifest')) and value.get('config') == 'sha256:' + value['id']
              and type(value.get('layers')) is list and value['layers'] and all(_digest(x) for x in value['layers'])
              and type(value.get('tags')) is list and len(value['tags']) <= 256
              and all(type(x) is str and 0 < len(x) <= 512 for x in value['tags'])
              and len(set(value['tags'])) == len(value['tags']), 'resource_changed')
        if selected:
            _need(value.get('os') == 'linux' and value.get('arch') == binding['target_arch']
                  and value.get('source') == binding['source'] and value.get('version') == binding['version'], 'resource_changed')
    elif kind == 'network':
        _need(_NAME.fullmatch(value.get('name', '')) and value.get('driver') == 'bridge'
              and value.get('internal') is True
              and value.get('options') == {} and value.get('listeners') == []
              and type(value.get('subnet')) is str, 'resource_changed')
        # No engine default network, host networking or unspecified topology.
        from ipaddress import ip_network
        try:
            net = ip_network(value['subnet'], strict=True)
            _need(net.version == 4 and net.is_private and not net.is_loopback, 'resource_changed')
        except ValueError:
            raise owner.OwnerRefusal('resource_changed') from None
    else:
        _container(value, binding)


def _vector(operations, mode, binding):
    result = []
    fields = {'target.core.postgres.value.path': 'core_executable',
              'target.core.initdb.value.path': 'core_initdb',
              'config.roles.bootstrap.binding.value.sql_name': 'bootstrap_role',
              'config.operation_id': 'operation_id'}
    for part in operations['operation_vectors'][mode]['argv']:
        if type(part) is dict:
            _need(set(part) == {'field'} and part['field'] in fields, 'required_binding_gap')
            key = fields[part['field']]
            part = binding.get(key)
            if key in ('core_executable', 'core_initdb'):
                _need(_path(part), 'required_binding_gap')
            elif key == 'bootstrap_role':
                _need(type(part) is str and re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', part), 'required_binding_gap')
            else:
                from uuid import UUID
                try:
                    _need(str(UUID(part)) == part, 'required_binding_gap')
                except (ValueError, TypeError, AttributeError):
                    raise owner.OwnerRefusal('required_binding_gap') from None
        result.append(part)
    return result


def _generated(kind, *, starting=False):
    # Only a new resource or activation has an unobserved generated identity.
    # Existing preimages always remain exact.
    if starting:
        return {('process',)}
    return ({('id',)} if kind in ('container', 'network') else
            {('created_at',), ('mountpoint', 'dev'), ('mountpoint', 'ino')} if kind == 'volume' else set())


def _expectation(kind, value, binding, *, creating, starting=False):
    candidate = thaw(_copy(value))
    if creating or starting:
        for path in _generated(kind, starting=starting):
            parent = candidate
            for key in path[:-1]:
                parent = parent[key]
            if parent[path[-1]] is None:
                parent[path[-1]] = ({'boot': binding['boot'], 'pid': 1, 'start': 1,
                                     'uid': binding['owner_uid']} if path == ('process',) else
                                    '0'*64 if path == ('id',) else
                                    'unobserved' if path == ('created_at',) else 1)
    _resource(kind, candidate, binding)


def _matches(kind, actual, expected, *, creating, starting=False):
    a = thaw(actual); e = thaw(expected)
    if a is None or e is None:
        return a is e
    if creating or starting:
        for path in _generated(kind, starting=starting):
            ap = a; ep = e
            for key in path[:-1]:
                ap = ap[key]; ep = ep[key]
            if starting or ep[path[-1]] is None:
                ep[path[-1]] = ap[path[-1]]
    return canonical_bytes(a) == canonical_bytes(e)


def _container(v, b):
    _need(v.get('role') in ('core', 'api', 'helper') and _NAME.fullmatch(v.get('name', ''))
          and _id(v.get('image')) and (_id(v.get('network')) or v.get('network') is None and v.get('running') is False)
          and v.get('uid') == b['owner_uid'] and v.get('gid') == b['owner_uid']
          and v.get('uid_map') == [[b['owner_uid'], b['owner_uid'], 1]]
          and v.get('read_only') is True and v.get('cap_drop') == ['ALL']
          and v.get('no_new_privileges') is True and v.get('privileged') is False
          and v.get('engine_access') == [], 'resource_changed')
    _need(v.get('ports') == ([] if v['role'] != 'api' else ['127.0.0.1:8000:8000']), 'resource_changed')
    operations = load_contracts().operations
    modes = {'core': ['core-server'], 'api': ['api'], 'helper': ['inspect', 'migrate', 'core-init']}[v['role']]
    vectors = []
    for mode in modes:
        try:
            vectors.append(_vector(operations, mode, b))
        except owner.OwnerRefusal:
            continue
    _need(v.get('argv') in vectors, 'resource_changed')
    limits = v.get('resources')
    _need(type(limits) is dict and set(limits) == {'cpu_millicores', 'memory_bytes', 'pids'}
          and all(_integer(x) for x in limits.values()) and limits['pids'] <= 4096, 'resource_changed')
    mounts = v.get('mounts')
    _need(type(mounts) is list and mounts and len(mounts) <= 16, 'resource_changed')
    targets = []
    for m in mounts:
        _need(type(m) is dict and set(m) == {'source', 'target', 'read_only'}
              and type(m['source']) is str and _NAME.fullmatch(m['source'])
              and _path(m['target']) and type(m['read_only']) is bool, 'resource_changed')
        _need(m['target'] == '/var/lib/cortex/core' or m['target'].startswith('/run/cortex/'), 'resource_changed')
        _need(m['target'] == '/var/lib/cortex/core' or m['read_only'], 'resource_changed')
        targets.append(m['target'])
    _need(len(targets) == len(set(targets)), 'resource_changed')
    _need(type(v.get('running')) is bool and type(v.get('ready')) is bool
          and (not v['ready'] or v['running']), 'resource_changed')
    if v['running']:
        _need(_lifetime(v.get('process'), b), 'resource_changed')
    else:
        _need(v.get('process') is None and not v['ready'], 'resource_changed')


def _plan(p, b):
    _need(type(p) is dict and p.get('kind') in _ACTIONS
          and p.get('action') in _ACTIONS[p['kind']])
    _need(_integer(p.get('deadline_ns')) and _integer(p.get('c2_ns'))
          and p['c2_ns'] <= p['deadline_ns'])
    _need(type(p.get('subject')) is dict and set(p['subject']) == {'identity', 'effect', 'request_ref'})
    _need(type(p['subject']['effect']) is dict
          and p['subject']['effect'].get('resource_kind') == p['kind'], 'generation_changed')
    _need(p.get('ownership') in ('created_by_this_transaction', 'verified_owned_continuation', 'external_read_only'))
    _need(p.get('data_class') in ('ephemeral', 'authoritative', 'backup', 'credentials'))
    _need(type(p.get('inverse')) is bool and type(p.get('dependents')) is list
          and all(_id(x) for x in p['dependents']))
    _need(p.get('role') in ('core', 'api', 'helper') and p.get('signal') in ('TERM', 'KILL'))
    _need(p.get('helper') in ('inspect', 'status', 'migrate', 'core-init'))
    for which in ('before', 'after'):
        r = p.get(which)
        if r is not None:
            _expectation(p['kind'], r, b, creating=which == 'after' and p.get('before') is None,
                         starting=which == 'after' and p['kind'] == 'container' and p['action'] == 'start')
    _need(p.get('before') is not None or p.get('after') is not None)
    if p['action'] in ('create', 'load'):
        _need(p['before'] is None and p['after'] is not None, 'resource_changed')
    else:
        _need(p['before'] is not None, 'resource_changed')
    _need((p['after'] is None) == (p['action'] == 'remove'), 'resource_changed')
    _need(p['name'] == (p['after'] or p['before']).get('name', (p['after'] or p['before']).get('id')), 'resource_changed')
    if p['action'] == 'remove':
        _need(p['inverse'] and p['ownership'] == 'created_by_this_transaction'
              and p['data_class'] == 'ephemeral' and p['before']['consumers'] == [], 'scope_denied')
        if p['kind'] == 'image':
            _need(p['before']['tags'] == [], 'scope_denied')
        if p['kind'] == 'container':
            _need(not p['before']['running'], 'scope_denied')
    elif p['ownership'] == 'external_read_only':
        raise owner.OwnerRefusal('scope_denied')
    pair = p.get('image_pair')
    _need(type(pair) is list and len(pair) == 2 and pair[0]['id'] != pair[1]['id'])
    for image in pair:
        _resource('image', image, b)
    if p['kind'] == 'image' and p['action'] == 'load':
        _need(p['after'] in pair and _path(p.get('oci'))
              and p['oci'].startswith(str(PurePosixPath(b['environment']['CONTAINERS_CONF']).parent) + '/')
              and p['oci'] in [x['path'] for x in b['closure']], 'custody_changed')
        _need(p['after']['tags'] == [], 'scope_denied')
    if p['kind'] == 'image':
        inventory = p.get('image_inventory')
        _need(type(inventory) is list and len(inventory) <= 256, 'resource_changed')
        for image in inventory:
            _resource('image', image, b, selected=False)
        _need(len({i['id'] for i in inventory}) == len(inventory), 'resource_changed')
        _need((p['before'] is not None and p['before'] in inventory) or
              (p['before'] is None and all(i['id'] != p['after']['id'] for i in inventory)), 'resource_changed')
    if p['kind'] == 'container':
        for v in (p['before'], p['after']):
            if v is not None:
                core = v['role'] == 'core' or v['role'] == 'helper' and p['helper'] == 'core-init'
                _need(v['role'] == p['role'] and v['image'] == pair[0 if core else 1]['id'], 'resource_changed')
        if p['before'] is not None and p['after'] is not None:
            _need(p['before']['id'] == p['after']['id'], 'resource_changed')
            mutable = {'running', 'ready', 'process'} if p['action'] in ('start', 'stop', 'signal') else set()
            _need({k:v for k,v in p['before'].items() if k not in mutable}
                  == {k:v for k,v in p['after'].items() if k not in mutable}, 'resource_changed')
        if p['action'] == 'start':
            _need(not p['before']['running'] and p['after']['running'], 'resource_changed')
        if p['action'] in ('stop', 'signal'):
            _need(p['before']['running'] and not p['after']['running'], 'resource_changed')
        if p['action'] == 'helper':
            if p['role'] == 'api':
                _need(p['before']['running'] and p['helper'] in ('inspect', 'status'), 'resource_changed')
            else:
                _need(p['role'] == 'helper' and not p['before']['running']
                      and p['helper'] != 'status'
                      and p['before']['argv'] == _vector(load_contracts().operations, p['helper'], b), 'resource_changed')
        _dependencies(p, b)
    elif p['kind'] == 'network' and p['action'] in ('connect', 'disconnect'):
        _need({k:v for k,v in p['before'].items() if k != 'consumers'}
              == {k:v for k,v in p['after'].items() if k != 'consumers'}, 'resource_changed')
        _dependencies(p, b)


def _dependencies(p, b):
    """Exact original resource graph used by container operations; no name trust."""
    deps = p.get('dependencies')
    _need(type(deps) is list and 0 < len(deps) <= 18, 'resource_changed')
    if p['kind'] == 'network':
        old = set(p['before']['consumers']); new = set(p['after']['consumers'])
        changed = new - old if p['action'] == 'connect' else old - new
        _need(len(changed) == 1 and (old <= new if p['action'] == 'connect' else new <= old)
              and len(deps) == 1 and deps[0]['kind'] == 'container', 'resource_changed')
        c = deps[0]['resource']; _resource('container', c, b)
        _need(c['id'] == next(iter(changed)) and not c['running']
              and c['network'] == (None if p['action'] == 'connect' else p['before']['id']), 'resource_changed')
        return
    v = p['before'] or p['after']
    needed = {('image', v['image']), ('network', v['network'])}
    needed |= {('volume', m['source']) for m in v['mounts']}
    found = []
    for dep in deps:
        _need(type(dep) is dict and set(dep) == {'kind', 'resource'}
              and dep['kind'] in ('image', 'network', 'volume'), 'resource_changed')
        kind = dep['kind']; r = dep['resource']
        _resource(kind, r, b)
        found.append((kind, r['name'] if kind == 'volume' else r['id']))
    _need(len(found) == len(set(found)) and set(found) == needed, 'resource_changed')


def _argv(p, b):
    """Only finite literal vectors; names locate, full held identities control."""
    argv = [b['executable'], '--remote=false', '--root', b['root'],
            '--runroot', b['runroot'], '--tmpdir', b['tmpdir']]
    kind = p['kind']; action = p['action']; v = p['after'] or p['before']
    target = v['name'] if kind == 'volume' else v['id']
    if kind == 'image':
        argv += ['load', '--input', p['oci']] if action == 'load' else ['image', 'rm', target]
    elif kind == 'volume':
        argv += ['volume', 'create', '--driver=local', v['name']] if action == 'create' else ['volume', 'rm', target]
    elif kind == 'network':
        if action == 'create':
            argv += ['network', 'create', '--internal', '--driver=bridge', '--subnet=' + v['subnet'], v['name']]
        elif action == 'remove':
            argv += ['network', 'rm', target]
        else:
            old = set(p['before']['consumers']); new = set(p['after']['consumers'])
            change = (new - old) if action == 'connect' else (old - new)
            _need(len(change) == 1 and (old <= new if action == 'connect' else new <= old), 'resource_changed')
            argv += ['network', action, target, next(iter(change))]
    elif action == 'create':
        argv += ['create', '--name=' + v['name'], '--pull=never', '--read-only',
                 '--read-only-tmpfs=false', '--image-volume=ignore', '--no-healthcheck',
                 '--log-driver=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                 '--user=' + str(v['uid']) + ':' + str(v['gid']), '--network=' + v['network'],
                 '--pids-limit=' + str(v['resources']['pids']), '--unsetenv-all']
        argv += cpu_memory_argv(v['resources']['cpu_millicores'], v['resources']['memory_bytes'])
        for m in v['mounts']:
            argv += ['--mount=type=volume,src=' + m['source'] + ',dst=' + m['target']
                     + (',ro' if m['read_only'] else ',rw')]
        for port in v['ports']:
            argv += ['--publish=' + port]
        argv += ['sha256:' + v['image'], *v['argv']]
    elif action == 'helper':
        _need(p['helper'] != 'status' or v['role'] == 'api', 'scope_denied')
        if v['role'] == 'helper':
            argv += ['start', '--attach', '--sig-proxy=false', target]
        else:
            argv += ['exec', '--user=' + str(v['uid']) + ':' + str(v['gid']), target,
                     *_vector(load_contracts().operations, p['helper'], b)]
    elif action == 'signal':
        argv += ['kill', '--signal=' + p['signal'], target]
    elif action == 'stop':
        # Send a finite signal, never ask Podman for a fresh relative grace.
        argv += ['kill', '--signal=TERM', target]
    else:
        argv += [('rm' if action == 'remove' else 'start'), target]
    return tuple(argv)


def _installed_engine():
    # No real engine/clock/socket/host/default and no candidate access here.
    raise owner.OwnerRefusal('required_binding_gap')


@contextmanager
def _open():
    engine = _installed_engine()
    with engine.hold():
        try:
            binding, now = engine.current()
            _binding(binding)
            plan = thaw(_copy(engine.plan))
            _plan(plan, binding)
            primitive = _Primitive(engine, binding, plan, now)
        except owner.OwnerRefusal:
            raise
        except Exception:
            raise owner.OwnerRefusal('backend_unavailable') from None
        try:
            yield primitive
        finally:
            primitive.closed = True


class _Primitive:
    def __init__(self, engine, binding, plan, now):
        self.engine = engine
        self.binding = _copy(binding)
        self.plan = _copy(plan)
        self.now = now
        self.preimage = None
        self.snapshotted = False
        self.attempted = False
        self.closed = False
        self.postimage_held = False
        self.postimage = None

    def _current(self):
        _need(not self.closed, 'custody_changed')
        b, now = self.engine.current()
        _binding(b)
        _need(b == thaw(self.binding), 'custody_changed')
        _need(_integer(now, 0) and _integer(self.now, 0) and now >= self.now, 'time_unavailable')
        self.now = now
        _need(now < self.plan['deadline_ns'], 'reservation_expired')

    def _observe(self):
        self._current()
        p = thaw(self.plan)
        before = p['before'] or p['after']
        target = before['name'] if p['kind'] == 'volume' or p['before'] is None and p['kind'] in ('network', 'container') else before['id']
        value = self.engine.observe(p['kind'], target, deadline_ns=p['deadline_ns'])
        self._current()  # Collector IO/waits may revoke authority or the bound.
        if value is not None:
            _resource(p['kind'], value, thaw(self.binding))
        return _copy(value)

    def _related(self, actual, *, after):
        p = thaw(self.plan)
        result = []
        for dep in p.get('dependencies', []):
            kind = dep['kind']; expected = thaw(_copy(dep['resource']))
            if after and p['kind'] == 'container':
                consumers = set(expected['consumers'])
                if p['before'] is not None:
                    consumers.discard(p['before']['id'])
                if actual is not None:
                    consumers.add(actual['id'])
                expected['consumers'] = sorted(consumers)
            elif after and p['kind'] == 'network':
                expected['network'] = p['before']['id'] if p['action'] == 'connect' else None
            target = expected['name'] if kind == 'volume' else expected['id']
            self._current()
            observed = self.engine.observe(kind, target, deadline_ns=p['deadline_ns'])
            self._current()
            _resource(kind, observed, thaw(self.binding))
            _need(canonical_bytes(observed) == canonical_bytes(expected), 'resource_changed')
            result.append({'kind': kind, 'resource': observed})
        return result

    def _images(self, actual, *, after):
        p = thaw(self.plan)
        if p['kind'] != 'image':
            return None
        expected = p['image_inventory']
        if after:
            target = (p['before'] or p['after'])['id']
            expected = [v for v in expected if v['id'] != target]
            if actual is not None:
                expected.append(thaw(actual))
        self._current()
        observed = self.engine.observe('image_inventory', None, deadline_ns=p['deadline_ns'])
        self._current()
        _need(type(observed) is list and len(observed) <= 256, 'resource_changed')
        for image in observed:
            _resource('image', image, thaw(self.binding), selected=False)
        key = lambda v: v['id']
        _need(canonical_bytes(sorted(observed, key=key)) == canonical_bytes(sorted(expected, key=key)), 'resource_changed')
        return observed

    def snapshot(self):
        _need(not self.snapshotted and not self.attempted, 'reservation_used')
        actual = self._observe()
        _need(actual == self.plan['before'], 'resource_changed')
        self._related(thaw(actual), after=False)
        self._images(actual, after=False)
        self.preimage = actual
        self.snapshotted = True
        # Private non-SQL facts, deliberately NOT forged into SQL's five slots.
        return freeze({'resource': actual, 'backend': self.binding})

    def dispatch(self, context):
        _need(self.snapshotted and not self.attempted, 'reservation_used')
        _need(thaw(context) == thaw(self.plan['subject']), 'generation_changed')
        _need(self._observe() == self.preimage, 'resource_changed')
        self._related(thaw(self.preimage), after=False)
        self._images(self.preimage, after=False)
        p = thaw(self.plan); b = thaw(self.binding)
        if p['kind'] == 'container' and p['role'] == 'core' and p['action'] in ('stop', 'signal'):
            _need(self.now >= p['c2_ns'], 'attempt_ambiguous')
            for dependent in p['dependents']:
                absent = self.engine.observe('container', dependent, deadline_ns=p['deadline_ns'])
                self._current()
                _need(absent is None, 'attempt_ambiguous')
            self._cleanup(self.engine.witnesses())
        argv = _argv(p, b)
        self._current()
        self.attempted = True  # Ambiguous failed/lost replies never permit retry.
        try:
            self.engine.run(argv, env=dict(b['environment']), deadline_ns=p['deadline_ns'],
                            stdout_limit=_LIMIT, stderr_limit=_LIMIT)
        except Exception:
            # The independent observation decides. Never turn timeout into absence.
            pass

    def _cleanup(self, witness):
        _need(type(witness) is dict and {'cleanup', 'creation'} <= set(witness), 'attempt_ambiguous')
        c = witness['cleanup']; b = thaw(self.binding)
        _need(type(c) is dict and c.get('remaining') == [] and c.get('reaped') is True
              and c.get('pipes_closed') is True and _lifetime(c.get('lifetime'), b)
              and c['lifetime'] != b['owner_lifetime']
              and all(type(c.get(k)) is bytes and len(c[k]) <= _LIMIT for k in ('stdout', 'stderr'))
              and type(c.get('returncode')) is int, 'attempt_ambiguous')
        return c

    @contextmanager
    def measure(self, context):
        answer = ('UNKNOWN', None)
        try:
            _need(self.snapshotted and thaw(context) == thaw(self.plan['subject']), 'generation_changed')
            actual = self._observe()
            witness = thaw(freeze(self.engine.witnesses()))
            cleanup = self._cleanup(witness)
            p = thaw(self.plan)
            # Every verified outcome must join cleanup to the original attempt.
            _need(self.attempted and witness.get('execution') == {
                'argv': list(_argv(p, thaw(self.binding))), 'subject': p['subject'],
                'lifetime': cleanup['lifetime'], 'deadline_ns': p['deadline_ns']}, 'attempt_ambiguous')
            post_matches = _matches(p['kind'], actual, self.plan['after'], creating=p['before'] is None,
                                    starting=p['kind'] == 'container' and p['action'] == 'start')
            # Complete unchanged graphs prove NOT_APPLIED; mixed graphs do not.
            related = self._related(thaw(actual), after=post_matches)
            images = self._images(actual, after=post_matches)
            self._current()
            state = None
            if post_matches:
                creation = witness['creation']
                if p['before'] is None:
                    _need(creation == {'before_absent': True, 'subject': p['subject'],
                        'resource': thaw(actual), 'exclusion': thaw(self.binding)['exclusion']}, 'attempt_ambiguous')
                # Identical before/after cannot prove execution from object state.
                if self.plan['before'] != self.plan['after']:
                    state = 'APPLIED'
                elif self.attempted and p['action'] == 'helper' and cleanup['returncode'] == 0:
                    state = 'APPLIED'
            if state is None and actual == self.preimage and self.plan['before'] != self.plan['after']:
                state = 'NOT_APPLIED'
            if state is not None:
                if state == 'APPLIED':
                    if self.postimage_held:
                        _need(actual == self.postimage, 'resource_changed')
                    else:
                        self.postimage = actual
                        self.postimage_held = True
                result = {**p['subject'], 'state': {'APPLIED': 'applied_verified',
                    'NOT_APPLIED': 'not_applied_verified'}[state], 'observed': {
                    'resource': thaw(actual), 'backend': thaw(self.binding),
                    'related': related,
                    'image_inventory': images,
                    'finite_child': {k: cleanup[k] for k in ('lifetime', 'remaining', 'reaped', 'pipes_closed')}}}
                answer = (state, canonical_bytes(result))
        except Exception:
            answer = ('UNKNOWN', None)
        # Deliberately outside the try: failed publication must propagate, never ACK.
        yield answer
        if answer[0] != 'UNKNOWN':
            self._current()  # Held exclusion survives durable verified publication.
            _need(self._observe() == actual, 'resource_changed')
            self._related(thaw(actual), after=state == 'APPLIED')
            self._images(actual, after=state == 'APPLIED')
            current_witness = self.engine.witnesses()
            self._cleanup(current_witness)
            _need(current_witness == witness, 'attempt_ambiguous')
            self._current()  # Final collector IO may expire the original bound or revoke custody.
