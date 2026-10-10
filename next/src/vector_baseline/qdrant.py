"""Owned, no-published-port Qdrant benchmark arm; never an existing service."""
import asyncio
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from . import corpus, geometry, postgres, qdrant_proxy

VERSION = '1.19.2'
PINS = {'amd64': 'docker.io/qdrant/qdrant@sha256:0e8273b9130ca3b0dd9dcfaa8f55342066711f8aaeb2aa072e53344c42ecf57f',
        'arm64': 'docker.io/qdrant/qdrant@sha256:9eb80ba088703193c124db5e50a59f2028439c8bbc62fa1918625995116b50d0'}
CLIENT_PINS = {'amd64': 'docker.io/library/python@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe',
               'arm64': 'docker.io/library/python@sha256:16bf2b5c59a08523519d3c8589deca849285dee1d62a3653e124c3dba340f15e'}
SETTINGS = {'m': 16, 'ef_construct': 64, 'hnsw_ef': 200, 'exact': False}


def configuration(key):
    return {'log_level': 'ERROR', 'telemetry_disabled': True, 'cluster': {'enabled': False},
            'storage': {'storage_path': '/qdrant/storage', 'snapshots_path': '/qdrant/storage/snapshots'},
            'service': {'host': '0.0.0.0', 'http_port': 6333, 'grpc_port': None, 'api_key': key,
                        'enable_cors': False, 'enable_tls': False, 'enable_snapshot_url_recovery': False}}


def check_preflight(free_percent, inventory):
    if type(free_percent) is not int or free_percent < 35 or not isinstance(inventory, str) or inventory.strip():
        raise RuntimeError('test stack needs >=35% free memory and the exclusive team slot')


def verify_container(row, *, memory_bytes, cpus):
    config, host = row['Config'], row['HostConfig']
    nano = host.get('NanoCpus', 0)
    observed_cpus = nano / 1_000_000_000 if nano else host.get('CpuQuota', 0) / (host.get('CpuPeriod', 0) or 100000)
    caps = 'ALL' in host.get('CapDrop', []) or row.get('EffectiveCaps') == []
    no_privilege = any(x.startswith('no-new-privileges') for x in host.get('SecurityOpt', []))
    if (config.get('User') != '10001:10001' or any(host.get('PortBindings', {}).values())
            or host.get('ReadonlyRootfs') is not True or host.get('Memory') != memory_bytes
            or abs(observed_cpus - cpus) > 1e-9 or not caps or not no_privilege
            or any(x.startswith('QDRANT__SERVICE__API_KEY=') for x in config.get('Env', []))):
        raise RuntimeError('observed owned container violates the reviewed delivery contract')
    return {'uid': 10001, 'published_ports': False, 'read_only_root': True, 'memory_bytes': host['Memory'],
            'cpus': observed_cpus, 'cap_drop_all': caps, 'no_new_privileges': no_privilege, 'key_in_env': False}


def preflight(runner=postgres.podman, *, allowed_names=()):
    if sys.platform == 'darwin':
        memory = subprocess.check_output(['memory_pressure'], text=True)
        found = re.search(r'System-wide memory free percentage:\s*(\d+)%', memory)
        if not found:
            raise RuntimeError('memory observation unavailable')
        free = int(found[1]); source = 'memory_pressure'
    else:
        fields = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        free = fields['MemAvailable'] * 100 // fields['MemTotal']; source = '/proc/meminfo MemAvailable'
    inventory = runner(['ps', '-a', '--filter', 'label=cortex.test', '--format', '{{.Names}}'])
    foreign = '\n'.join(n for n in inventory.splitlines() if n not in allowed_names)
    check_preflight(free, foreign)
    return {'utc': datetime.now(timezone.utc).isoformat(), 'free_percent': free, 'memory_source': source,
            'team_inventory': inventory, 'allowed_owned_names': list(allowed_names), 'foreign_inventory': foreign}


class DisposableQdrant:
    def __init__(self, *, architecture=None, runner=postgres.podman, gate=None, lock_path=None):
        if architecture is not None and architecture not in PINS:
            raise ValueError('unsupported pinned platform')
        self.architecture, self.runner, self.gate = architecture, runner, gate
        ordinal = str(1 + secrets.randbits(64))
        self.name = 'kaidera-dev-vector-qdrant-' + ordinal
        self.client_name = 'kaidera-dev-vector-client-' + ordinal
        self.network_name = 'kaidera-dev-vector-network-' + ordinal
        self.secret_name = 'kaidera-dev-vector-config-' + ordinal
        self.lifecycle = secrets.token_hex(16)
        self.owned, self.pending, self.targets = set(), set(), {}
        self.password = self.lock = None
        self.used = self.cleanup_verified = False
        self.binding, self.id_map, self.preflights = {}, {}, []
        self.lock_path = Path(lock_path) if lock_path is not None else Path.home()/'.cache/kaidera/b01-podman.lock'

    def labels(self):
        return ['--label', 'worker=nemo', '--label', 'cortex.worker=nemo', '--label', 'cortex.test='+self.lifecycle,
                '--label', 'kaidera.b02.lifecycle='+self.lifecycle]

    def network_args(self):
        return ['network', 'create', '--internal', *self.labels(), self.network_name]

    def run_args(self, role):
        if role not in ('qdrant', 'client') or self.architecture not in PINS:
            raise ValueError('explicit pinned role/platform required')
        common = ['create', '--name', self.name if role == 'qdrant' else self.client_name, *self.labels(),
                  '--user', '10001:10001', '--init', '--read-only', '--cap-drop=ALL',
                  '--security-opt=no-new-privileges', '--pids-limit=128', '--network', self.network_name,
                  '--tmpfs', '/tmp:rw,size=16m,mode=1777', '--secret',
                  self.secret_name+',type=mount,target=qdrant.yaml,uid=10001,gid=10001,mode=0400']
        if role == 'qdrant':
            return [*common, '--network-alias', 'qdrant', '--memory=768m', '--cpus=1.5',
                    '--tmpfs', '/qdrant/storage:rw,size=512m,uid=10001,gid=10001,mode=0700',
                    PINS[self.architecture], '/qdrant/qdrant', '--config-path', '/run/secrets/qdrant.yaml']
        return [*common, '--memory=256m', '--cpus=0.5', CLIENT_PINS[self.architecture],
                'python', '-c', 'import time;time.sleep(86400)']

    def owned_resource(self, kind, name=None):
        name = name or self.name
        args = ['ps', '-a', '--format', '{{.Names}}'] if kind == 'container' else [kind, 'ls', '--format', '{{.Name}}']
        if name not in self.runner(args).splitlines():
            return None
        rows = json.loads(self.runner([kind, 'inspect', name]))
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError('ownership inspection incomplete')
        row = rows[0]
        if kind == 'container':
            observed_name, labels, target = row['Name'].removeprefix('/'), row['Config']['Labels'], row['Id']
        elif kind == 'secret':
            observed_name, labels, target = row['Spec']['Name'], row['Spec'].get('Labels'), row['ID']
        else:
            observed_name, labels, target = row['name'], row.get('labels'), row['id']
        if observed_name != name or (labels or {}).get('kaidera.b02.lifecycle') != self.lifecycle:
            return None
        if not isinstance(target, str) or not target or any(x.isspace() for x in target):
            raise RuntimeError('immutable resource target missing')
        if (kind, name) in self.targets and self.targets[kind, name] != target:
            raise RuntimeError('owned resource identity changed')
        return target

    def create(self, kind, name, args, **kwargs):
        self.pending.add((kind, name))  # Effect may happen before its acknowledgement.
        self.runner(args, **kwargs)
        target = self.owned_resource(kind, name)
        if target is None:
            raise RuntimeError('create did not establish owned identity')
        self.targets[kind, name] = target
        self.owned.add((kind, name)); self.pending.remove((kind, name))
        return target

    def admit_start(self):
        if self.gate is not None:
            value = self.gate(self.runner)
        else:
            allowed = [name for kind, name in self.owned if kind == 'container'
                       and self.owned_resource(kind, name) is not None]
            value = preflight(self.runner, allowed_names=allowed)
        if value is not None:
            self.preflights.append(value)

    def __enter__(self):
        if self.used:
            raise ValueError('single-use disposable lifecycle')
        self.used = True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.lock_path.open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.admit_start()
            observed = self.runner(['info', '--format', '{{.Host.Arch}}'])
            if observed not in PINS or self.architecture is not None and self.architecture != observed:
                raise ValueError('runtime architecture differs from admitted pins')
            self.architecture = observed
            self.password = secrets.token_urlsafe(32)
            self.create('network', self.network_name, self.network_args())
            self.create('secret', self.secret_name, ['secret', 'create', *self.labels(), self.secret_name, '-'],
                        input=json.dumps(configuration(self.password)))
            q = self.create('container', self.name, self.run_args('qdrant'))
            client = self.create('container', self.client_name, self.run_args('client'))
            self.admit_start(); self.runner(['start', q])
            self.admit_start(); self.runner(['start', client])
            observed_contracts = {}
            for role, target, memory, cpus in [('qdrant', q, 768 * 1024**2, 1.5), ('client', client, 256 * 1024**2, .5)]:
                row = json.loads(self.runner(['container', 'inspect', target]))[0]
                observed_contracts[role] = verify_container(row, memory_bytes=memory, cpus=cpus)
            self.binding = {'engine': 'qdrant', 'version_pin': VERSION, 'image': PINS[observed],
                            'client_image': CLIENT_PINS[observed], 'architecture': observed, 'lifecycle': self.lifecycle,
                            'container_ids': [q, client], 'limits': {'memory_bytes': 1073741824, 'cpus': 2},
                            'uid': 10001, 'published_ports': False, 'network_internal': True,
                            'settings': SETTINGS, 'preflights': self.preflights,
                            'proxy_source_sha256': corpus.digest(qdrant_proxy.__file__),
                            'observed_contracts': observed_contracts}
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        errors = []
        self.cleanup_verified = False
        resources = [('container', self.client_name), ('container', self.name),
                     ('secret', self.secret_name), ('network', self.network_name)]
        for kind, name in resources:
            if (kind, name) not in self.owned | self.pending:
                continue
            try:
                target = self.owned_resource(kind, name)
                if target is not None:
                    self.runner(['rm', '-f', target] if kind == 'container' else [kind, 'rm', target])
                self.owned.discard((kind, name)); self.pending.discard((kind, name))
            except Exception as error:
                errors.append(kind + ':' + type(error).__name__)
        try:
            remaining = [self.owned_resource(kind, name) for kind, name in resources]
            self.cleanup_verified = not errors and not any(remaining) and not self.owned and not self.pending
        except Exception as error:
            errors.append('inventory:' + type(error).__name__)
        if self.cleanup_verified:
            self.password = None
            if self.lock is not None:
                self.lock.close(); self.lock = None
        else:
            raise RuntimeError('owned cleanup incomplete; serialization/custody retained: '+','.join(errors))

    def __exit__(self, *args):
        self.close()

    def proxy_command(self):
        target = self.owned_resource('container', self.client_name)
        if target is None:
            raise RuntimeError('owned client identity missing')
        return ['podman', 'exec', '-i', target, 'python', '-u', '-c', Path(qdrant_proxy.__file__).read_text()]

    async def stop_proxy(self, pid):
        target = await asyncio.to_thread(self.owned_resource, 'container', self.client_name)
        if target is None:
            raise RuntimeError('owned proxy container identity lost')
        child = await asyncio.create_subprocess_exec('podman', 'exec', target, 'python', '-c',
                    'import os,signal,sys;os.kill(int(sys.argv[1]),signal.SIGTERM)', str(pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(child.wait(), 5)
            if child.returncode:
                raise RuntimeError('owned proxy stop refused')
        finally:
            if child.returncode is None:
                child.kill(); await child.wait()


def dense_request(q):
    if q['mode'] != 'dense':
        raise ValueError('geometry Qdrant arm is dense only')
    conditions = [{'key': k, 'match': {'value': q[k]}} for k in ('tenant', 'project', 'generation')]
    conditions.append({'key': 'deleted', 'match': {'value': False}})
    for column, lo, hi in [('ordinal', 'lo', 'hi'), ('time', 'time_lo', 'time_hi')]:
        bounds = {op: q[key] for op, key in [('gte', lo), ('lte', hi)] if q[key] is not None}
        if bounds:
            conditions.append({'key': column, 'range': bounds})
    if q['kind'] is not None:
        conditions.append({'key': 'kind', 'match': {'value': q['kind']}})
    return {'query': q['vector'], 'filter': {'must': conditions}, 'limit': 10,
            'params': {'hnsw_ef': 200, 'exact': False}, 'with_payload': ['comparison_id'], 'with_vector': False}


def decode_hits(response, mapping):
    points = response['result']['points']; hits = []
    if not isinstance(points, list) or len(points) > 10:
        raise ValueError('invalid hit inventory')
    for point in points:
        sid = mapping.get(point['id'])
        if sid is None or point['payload']['comparison_id'] != sid or sid in hits:
            raise ValueError('unknown/duplicate/mismatched point ID')
        hits.append(sid)
    return hits


class Client:
    def __init__(self, stack):
        self.stack = stack
        self.process, self.pid = None, None
        self.sequence = 0
        self.closed = False

    async def open(self):
        try:
            args = await asyncio.to_thread(self.stack.proxy_command)
            self.process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=qdrant_proxy.MAX_BYTES+1024)
            raw = await asyncio.wait_for(self.process.stdout.readline(), 5)
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('kind') != 'ready' or type(value.get('pid')) is not int or value['pid'] <= 0:
                raise ValueError('proxy readiness refused')
            self.pid = value['pid']
        except BaseException:
            await self.aclose()
            raise

    async def call(self, method, path, body=None):
        if self.closed or self.process is None or self.process.returncode is not None:
            raise RuntimeError('proxy is not open')
        self.sequence += 1; seq = self.sequence
        try:
            frame = json.dumps({'seq': seq, 'method': method, 'path': path, 'body': body}, allow_nan=False).encode()+b'\n'
            if len(frame) > qdrant_proxy.MAX_BYTES:
                raise ValueError('request exceeds protocol cap')
            self.process.stdin.write(frame); await self.process.stdin.drain()
            raw = await self.process.stdout.readline()
            value = json.loads(raw)
            if type(value.get('seq')) is not int or value['seq'] != seq:
                raise ValueError('proxy response sequence mismatch')
            if 'error' in value:
                raise RuntimeError('owned HTTP request failed')
            return value['body']
        except BaseException:
            await self.aclose()
            raise

    async def request(self, q):
        response = await self.call('POST', '/collections/b02/points/query', dense_request(q))
        return {'ids': decode_hits(response, self.stack.id_map), 'connection_id': self.pid}

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        if self.process is None:
            return
        errors = []
        if self.process.returncode is None:
            if self.pid is not None:
                try:
                    await self.stack.stop_proxy(self.pid)
                except Exception as error:
                    errors.append(type(error).__name__)
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill(); await self.process.wait()
        if errors:
            raise RuntimeError('owned proxy cleanup failed')


async def upload(client, c):
    version = await client.call('GET', '/')
    if version.get('version') != VERSION:
        raise ValueError('runtime Qdrant version drift')
    distance = {'cosine': 'Cosine', 'dot': 'Dot', 'euclidean': 'Euclid'}[c.identity['metric']]
    await client.call('PUT', '/collections/b02', {'vectors': {'size': c.identity['dimension'], 'distance': distance},
                      'hnsw_config': {'m': 16, 'ef_construct': 64}, 'optimizers_config': {'default_segment_number': 1}})
    for key in ('tenant', 'project', 'generation', 'deleted', 'ordinal', 'kind', 'time'):
        schema = 'bool' if key == 'deleted' else 'integer' if key in ('ordinal', 'kind', 'time') else 'keyword'
        await client.call('PUT', '/collections/b02/index?wait=true', {'field_name': key, 'field_schema': schema})
    mapping = {}
    for start in range(0, len(c.records), 64):
        points = []
        for index in range(start, min(start+64, len(c.records))):
            row = c.records[index]; sid = row['id'].decode(); mapping[index+1] = sid
            payload = {k: row[k].decode() for k in ('tenant', 'project')}
            payload.update(comparison_id=sid, generation=c.identity['generation'], deleted=bool(row['deleted']),
                           kind=int(row['kind']), time=int(row['time']), ordinal=int(row['ordinal']))
            points.append({'id': index+1, 'vector': c.vectors[index].tolist(), 'payload': payload})
        await client.call('PUT', '/collections/b02/points?wait=true', {'points': points})
    count = await client.call('POST', '/collections/b02/points/count', {'exact': True})
    if count['result']['count'] != len(c.records):
        raise ValueError('import count mismatch')
    info = await client.call('GET', '/collections/b02')
    return mapping, {'observed_version': version['version'], 'exact_count': count['result']['count'],
                     'collection': info['result'], 'hnsw_use_qualified': False}


def install(stack, c, *, admission=None):
    if c.manifest['dataset'] != 'synthetic':
        geometry.admit_artifact(c.manifest, admission)
    async def perform():
        deadline = time.monotonic()+45
        while True:
            client = Client(stack)
            try:
                await client.open()
                version = await client.call('GET', '/')
                if version.get('version') != VERSION:
                    raise ValueError('runtime Qdrant version drift')
            except RuntimeError:
                await client.aclose()
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(.25)
                continue
            except BaseException:
                await client.aclose()
                raise
            break
        try:
            # No retry after ANY collection/index/upload operation has started.
            return await upload(client, c)
        finally:
            await client.aclose()
    mapping, binding = asyncio.run(asyncio.wait_for(perform(), 120))
    stack.id_map = mapping
    stack.binding.update(binding)
    return binding
