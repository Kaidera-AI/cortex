"""Frozen API1-001 companion and leaf-batch regressions on owned scratch PG."""
import asyncio
from uuid import uuid4

import asyncpg
import pytest

from test_audit_data_integrity_native import api as frozen_api, scratch_conn as frozen_scratch_conn


@pytest.fixture(scope='module')
def api():
    yield from frozen_api.__wrapped__()


@pytest.fixture
def scratch_conn():
    yield from frozen_scratch_conn.__wrapped__()


async def tables(connection, api_module):
    columns = api_module._RETENTION_TABLES['handoffs']['cols'].split(', ')
    definitions = []
    for column in columns:
        kind = 'uuid' if column in ('id', 'reply_to_handoff_id') else 'timestamptz' if column.endswith('_at') else 'text'
        definitions.append(f'{column} {kind}')
    definition = ', '.join(definitions)
    await connection.execute(f'CREATE TABLE handoffs ({definition}, PRIMARY KEY (id))')
    await connection.execute('ALTER TABLE handoffs ADD FOREIGN KEY (reply_to_handoff_id) REFERENCES handoffs(id)')
    await connection.execute(f'CREATE TABLE archive_handoffs ({definition}, PRIMARY KEY (id))')


async def row(connection, identity, *, parent=None, age=30, status='completed', summary='source'):
    await connection.execute("INSERT INTO handoffs(id,reply_to_handoff_id,status,created_at,summary) VALUES($1,$2,$3,now()-($4::int * interval '1 day'),$5)",
                             identity, parent, status, age, summary)


def test_old_eligibility_is_a_body_assertion_not_fixture_error(api, scratch_conn):
    async def case():
        connection = await asyncpg.connect(**scratch_conn)
        try:
            await tables(connection, api)
            parent, child = uuid4(), uuid4()
            await row(connection, parent, age=30)
            await row(connection, child, parent=parent, age=29)
            cause, moved = None, None
            try:
                moved = await connection.fetchval(api.retention_move_sql('handoffs', 7, 1))
            except asyncpg.PostgresError as error:
                cause = type(error).__name__
            assert cause is None, f'leaf-only MOVE must not raise a product SQL error: {cause}'
            assert moved == 1
            assert await connection.fetchval('SELECT id FROM handoffs') == parent
            archived = await connection.fetchrow('SELECT id,reply_to_handoff_id,summary FROM archive_handoffs')
            assert (archived['id'], archived['reply_to_handoff_id'], archived['summary']) == (child, parent, 'source')
        finally:
            await connection.close()
    asyncio.run(case())


def test_deep_chain_moves_leaf_then_each_parent_without_fk_change(api, scratch_conn):
    async def case():
        connection = await asyncpg.connect(**scratch_conn)
        try:
            await tables(connection, api)
            parent, child, grandchild = uuid4(), uuid4(), uuid4()
            await row(connection, parent, age=40)
            await row(connection, child, parent=parent, age=30)
            await row(connection, grandchild, parent=child, age=20)
            for expected in (grandchild, child, parent):
                before = {r['id'] for r in await connection.fetch('SELECT id FROM handoffs')}
                assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 1
                after = {r['id'] for r in await connection.fetch('SELECT id FROM handoffs')}
                assert before - after == {expected}
            assert await connection.fetchval('SELECT count(*) FROM archive_handoffs') == 3
            assert await connection.fetchval('SELECT reply_to_handoff_id FROM archive_handoffs WHERE id=$1', grandchild) == child
        finally:
            await connection.close()
    asyncio.run(case())


@pytest.mark.parametrize('age,status', [(1, 'completed'), (29, 'pending'), (29, 'claimed')])
def test_recent_or_active_child_keeps_parent_and_thread_in_source(api, scratch_conn, age, status):
    async def case():
        connection = await asyncpg.connect(**scratch_conn)
        try:
            await tables(connection, api)
            parent, child = uuid4(), uuid4()
            await row(connection, parent, age=30)
            await row(connection, child, parent=parent, age=age, status=status)
            assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 0
            assert await connection.fetchval('SELECT count(*) FROM handoffs') == 2
            assert await connection.fetchval('SELECT count(*) FROM archive_handoffs') == 0
        finally:
            await connection.close()
    asyncio.run(case())


def test_archive_conflict_keeps_child_and_referenced_parent_without_loss(api, scratch_conn):
    async def case():
        connection = await asyncpg.connect(**scratch_conn)
        try:
            await tables(connection, api)
            parent, child = uuid4(), uuid4()
            await row(connection, parent, age=30)
            await row(connection, child, parent=parent, age=29)
            await connection.execute("INSERT INTO archive_handoffs(id,summary) VALUES($1,'existing')", child)
            assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 0
            assert await connection.fetchval('SELECT count(*) FROM handoffs') == 2
            assert await connection.fetchval('SELECT summary FROM archive_handoffs WHERE id=$1', child) == 'existing'
            assert await connection.fetchval('SELECT reply_to_handoff_id FROM handoffs WHERE id=$1', child) == parent
        finally:
            await connection.close()
    asyncio.run(case())


def test_batch_limit_is_preserved_while_siblings_then_parent_move(api, scratch_conn):
    async def case():
        connection = await asyncpg.connect(**scratch_conn)
        try:
            await tables(connection, api)
            parent, first, second = uuid4(), uuid4(), uuid4()
            await row(connection, parent, age=40)
            await row(connection, first, parent=parent, age=30)
            await row(connection, second, parent=parent, age=29)
            assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 1
            assert await connection.fetchval('SELECT count(*) FROM handoffs') == 2
            assert await connection.fetchval('SELECT count(*) FROM handoffs WHERE id=$1', parent) == 1
            assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 1
            assert await connection.fetchval('SELECT id FROM handoffs') == parent
            assert await connection.fetchval(api.retention_move_sql('handoffs', 7, 1)) == 1
        finally:
            await connection.close()
    asyncio.run(case())
