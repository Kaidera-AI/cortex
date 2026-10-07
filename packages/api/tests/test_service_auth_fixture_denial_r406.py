"""The canonical visibility fixture must preserve real history-only refusals."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import service_auth as auth
from test_service_auth import MemoryDB, issue


@pytest.mark.parametrize("phase", ("issue", "authenticate"))
def test_history_only_identity_never_gets_access_after_fixture_projection(phase):
    async def scenario():
        db = MemoryDB()
        now = [datetime(2026, 9, 5, tzinfo=timezone.utc)]
        store = auth.ServiceAuthStore(lambda: db, clock=lambda: now[0])
        context = (store, db, now)
        issued = await issue(context) if phase == "authenticate" else None
        before = deepcopy(db.data)
        original = db.token_row

        def history_only(token_id):
            row = original(token_id)
            return dict(row, agent_visibility="history-only") if row else row

        db.token_row = history_only
        with pytest.raises(auth.AuthInvalid):
            if phase == "issue":
                await issue(context)
            else:
                await store.authenticate(issued.raw_token)
        assert db.data == before

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ("issue", "authenticate"))
def test_identity_without_keep_visible_never_gets_access_after_fixture_projection(phase):
    async def scenario():
        db = MemoryDB()
        now = [datetime(2026, 9, 5, tzinfo=timezone.utc)]
        store = auth.ServiceAuthStore(lambda: db, clock=lambda: now[0])
        context = (store, db, now)
        issued = await issue(context) if phase == "authenticate" else None
        before = deepcopy(db.data)
        original = db.token_row

        def not_kept(token_id):
            row = original(token_id)
            return dict(row, agent_keep_visible="false") if row else row

        db.token_row = not_kept
        with pytest.raises(auth.AuthInvalid):
            if phase == "issue":
                await issue(context)
            else:
                await store.authenticate(issued.raw_token)
        assert db.data == before

    asyncio.run(scenario())
