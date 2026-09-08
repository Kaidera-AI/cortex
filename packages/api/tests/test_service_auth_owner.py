"""Real owner-private sockets; no API main, DB, container engine or network listener."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

import service_auth as auth
import service_auth_owner as owner
import service_auth_owner_client as client
from service_auth_owner_lifespan import owner_lifespan


spec = importlib.util.spec_from_file_location('owner_domain_fixture', Path(__file__).with_name('test_service_auth.py'))
domain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(domain)


@pytest_asyncio.fixture
async def rig():
    # Darwin's pathname socket limit is shorter than normal pytest paths.
    with tempfile.TemporaryDirectory(prefix='ctx-owner-', dir='/tmp') as temp:
        directory = Path(temp).resolve() / 'control'
        db = domain.MemoryDB()
        store = auth.ServiceAuthStore(lambda: db)
        calls = []

        async def resolve(conn, identity):
            calls.append(identity)
            assert conn.db is db
            return db.project_id, db.agent_id

        server = owner.OwnerServer(directory, store, resolve, timeout=0.3)
        async with server:
            yield server, db, calls


def request(instance=None, operation='identity', **fields):
    return {'protocol': owner.PROTOCOL, 'operation': operation, 'instance_id': instance, **fields}


async def call(server, value):
    return await client.request(server.socket_path, value, timeout=1)


async def wire(server, data):
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    writer.write(data)
    await writer.drain()
    result = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    try:
        await writer.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass  # Early refusal may close before the deliberately oversized write finishes.
    return result


def http(body, *, headers=b'', method=b'POST', path=b'/v1/owner'):
    return (method + b' ' + path + b' HTTP/1.1\r\nHost: cortex-owner\r\n'
            b'Content-Type: application/json\r\nContent-Length: ' + str(len(body)).encode()
            + b'\r\n' + headers + b'\r\n' + body)


@pytest.mark.asyncio
async def test_real_kernel_peer_and_one_use_setup(rig, capsys):
    server, db, calls = rig
    found = await call(server, request())
    instance = str(db.data['state']['instance_id'])
    assert found == {'protocol': owner.PROTOCOL, 'instance_id': instance, 'result': {'instance_id': instance}}
    assert server.directory.stat().st_mode & 0o777 == 0o700
    assert server.socket_path.lstat().st_mode & 0o777 == 0o600
    setup = await call(server, request(instance, 'setup-grant', purpose='bootstrap'))
    raw = setup['result']['raw_token']
    consume = request(instance, 'consume-setup', purpose='bootstrap', setup_token=raw,
                      identity={'project_key': 'notes', 'agent_name': 'writer'},
                      installation_id='owner-test', scopes=['memory:read'], revoke_existing=True)
    issued = await call(server, consume)
    assert (await server.store.authenticate(issued['result']['raw_token'])).scopes == {'memory:read'}
    with pytest.raises(owner.OwnerError):
        await call(server, consume)
    assert calls == [{'project_key': 'notes', 'agent_name': 'writer'}]
    assert capsys.readouterr() == ('', '')


@pytest.mark.asyncio
async def test_wrong_kernel_uid_is_refused_before_store(rig, monkeypatch):
    server, db, _ = rig
    monkeypatch.setattr(owner, 'peer_uid', lambda _: os.getuid() + 1)
    with pytest.raises(owner.OwnerError):
        await call(server, request())
    assert db.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['instance', 'operation', 'extra', 'purpose'])
async def test_identity_and_finite_operations_fail_closed(rig, change):
    server, db, _ = rig
    value = request(str(db.data['state']['instance_id']), 'setup-grant', purpose='bootstrap')
    if change == 'instance': value['instance_id'] = '00000000-0000-0000-0000-000000000000'
    if change == 'operation': value['operation'] = 'invalidate_restored_generations'
    if change == 'extra': value['authority'] = 'LOCAL_OWNER'
    if change == 'purpose': value['purpose'] = 'anything'
    with pytest.raises(owner.OwnerError):
        await call(server, value)
    assert not db.data['setups']


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['duplicate-json', 'oversize-body', 'duplicate-length', 'comma-length',
                                  'transfer', 'expect', 'upgrade', 'method', 'path', 'header', 'pipeline'])
async def test_real_http_parser_rejects_unsafe_wire_without_store(rig, damage):
    server, db, _ = rig
    body = json.dumps(request()).encode()
    headers = b''
    if damage == 'duplicate-json': body = b'{"operation":"identity","operation":"setup-grant"}'
    if damage == 'oversize-body': body = b'x' * (owner.MAX_BODY + 1)
    if damage == 'duplicate-length': headers = b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
    if damage == 'transfer': headers = b'Transfer-Encoding: chunked\r\n'
    if damage == 'expect': headers = b'Expect: 100-continue\r\n'
    if damage == 'upgrade': headers = b'Upgrade: websocket\r\n'
    if damage == 'header': headers = b'X-Long: ' + b'x' * owner.MAX_HEADER + b'\r\n'
    data = http(body, headers=headers, method=b'GET' if damage == 'method' else b'POST',
                path=b'/other' if damage == 'path' else b'/v1/owner')
    if damage == 'comma-length':
        length = str(len(body)).encode()
        data = data.replace(b'Content-Length: ' + length, b'Content-Length: ' + length + b',' + length)
    if damage == 'pipeline': data += http(body)
    response = await wire(server, data)
    assert b'200 ' not in response.split(b'\r\n', 1)[0]
    assert db.calls == []


@pytest.mark.asyncio
async def test_store_outage_is_sanitized_and_not_retried(rig, capsys):
    server, db, _ = rig
    db.data['state'] = None
    with pytest.raises(owner.OwnerError, match='unavailable'):
        await call(server, request())
    assert len(db.calls) == 1
    assert capsys.readouterr() == ('', '')


@pytest.mark.asyncio
async def test_timeout_and_shutdown_cancel_owned_tasks(rig):
    server, _db, _ = rig
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    writer.write(b'POST /v1/owner HTTP/1.1\r\n')
    await writer.drain()
    assert b'200 ' not in await asyncio.wait_for(reader.read(), 2)
    writer.close()
    await writer.wait_closed()
    await server.close()
    assert not server.tasks and not server.socket_path.exists()
    assert not (server.directory / 'owner.lock').exists()


@pytest.mark.asyncio
async def test_duplicate_listener_and_inode_replacement_preserved(rig):
    server, _db, _ = rig
    another = owner.OwnerServer(server.directory, server.store, server.resolve_identity)
    with pytest.raises(owner.OwnerError):
        await another.start()
    server.socket_path.unlink()
    server.socket_path.write_text('foreign replacement')
    await server.close()
    assert server.socket_path.read_text() == 'foreign replacement'


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['symlink', 'public', 'wrong-owner', 'stale'])
async def test_unsafe_control_paths_refused(rig, monkeypatch, damage):
    server, _db, _ = rig
    path = server.directory.parent / 'bad'
    if damage == 'symlink': path.symlink_to(server.directory)
    else:
        path.mkdir(mode=0o700)
        if damage == 'public': path.chmod(0o755)
        if damage == 'wrong-owner': monkeypatch.setattr(owner.os, 'getuid', lambda: -1)
        if damage == 'stale': (path / 'owner.sock').write_text('not a socket')
    with pytest.raises(owner.OwnerError):
        await owner.OwnerServer(path, server.store, server.resolve_identity).start()


@pytest.mark.asyncio
async def test_unselected_lifespan_composes_pool_and_socket_in_order(rig):
    server, _db, _ = rig
    path = server.directory.parent / 'adapter'
    events = []

    @asynccontextmanager
    async def original(app):
        events.append('pool-open')
        yield
        assert not (path / 'owner.sock').exists()
        events.append('pool-close')

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=original))
    wrapped = owner_lifespan(original, path, lambda: server.store, server.resolve_identity)
    assert app.router.lifespan_context is original  # no automatic activation
    async with wrapped(app):
        assert events == ['pool-open']
        assert (path / 'owner.sock').exists()
    assert events == ['pool-open', 'pool-close']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['setup-grant', 'consume-setup'])
async def test_instance_change_between_discovery_and_transaction_never_issues(rig, monkeypatch, operation):
    server, db, calls = rig
    instance = str(db.data['state']['instance_id'])
    value = request(instance, 'setup-grant', purpose='bootstrap')
    if operation == 'consume-setup':
        raw = (await call(server, value))['result']['raw_token']
        value = request(instance, operation, purpose='bootstrap', setup_token=raw,
                        identity={'project_key': 'notes', 'agent_name': 'writer'},
                        installation_id='owner-test', scopes=['memory:read'], revoke_existing=True)
    original = server.store.instance_identity

    async def changed(**kwargs):
        old = await original(**kwargs)
        db.data['state']['instance_id'] = uuid4()
        return old

    monkeypatch.setattr(server.store, 'instance_identity', changed)
    before = len(db.data['setups'])
    with pytest.raises(owner.OwnerError):
        await call(server, value)
    assert len(db.data['setups']) == before and not db.data['tokens'] and calls == []


@pytest.mark.asyncio
async def test_symlink_parent_refused_before_creating_directory(rig):
    server, _, _ = rig
    link = server.directory.parent / 'alias'
    link.symlink_to(server.directory)
    with pytest.raises(owner.OwnerError):
        await owner.OwnerServer(link / 'child', server.store, server.resolve_identity).start()
    assert not (server.directory / 'child').exists()


@pytest.mark.asyncio
async def test_request_timeout_rolls_back_pending_canonical_resolver(rig):
    server, db, _ = rig
    instance = str(db.data['state']['instance_id'])
    setup = await call(server, request(instance, 'setup-grant', purpose='bootstrap'))
    entered = asyncio.Event()

    async def blocked(conn, identity):
        entered.set()
        await asyncio.Event().wait()

    server.resolve_identity = blocked
    with pytest.raises(owner.OwnerError):
        await call(server, request(instance, 'consume-setup', purpose='bootstrap',
            setup_token=setup['result']['raw_token'], identity={'project_key': 'notes', 'agent_name': 'writer'},
            installation_id='owner-test', scopes=['memory:read'], revoke_existing=True))
    assert entered.is_set() and not db.data['tokens']
    assert all(grant['consumed_at'] is None for grant in db.data['setups'].values())


@pytest.mark.asyncio
async def test_concurrent_connection_limit_refuses_excess_and_shutdown_drains(rig):
    server, db, _ = rig
    server.max_connections = 1
    first_r, first_w = await asyncio.open_unix_connection(str(server.socket_path))
    await asyncio.sleep(0)
    with pytest.raises(owner.OwnerError):
        await call(server, request())
    assert len(server.tasks) <= 1 and not db.calls
    await server.close()
    assert not server.tasks and await first_r.read() == b''
    first_w.close()
    await first_w.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['null', 'nested-null', 'nested-other'])
async def test_real_peer_identity_response_requires_matching_nonnull_uuid(rig, monkeypatch, damage):
    server, _, _ = rig
    original = server._respond

    async def malformed(writer, conn, status, value):
        if status == 200:
            if damage == 'null': value['instance_id'] = None
            elif damage == 'nested-null': value['result']['instance_id'] = None
            else: value['result']['instance_id'] = str(uuid4())
        await original(writer, conn, status, value)

    monkeypatch.setattr(server, '_respond', malformed)
    with pytest.raises(owner.OwnerError):
        await call(server, request())
