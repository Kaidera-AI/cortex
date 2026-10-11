"""Negative control on the qualified disposable test role, after fixture setup."""
import os
import pytest_asyncio
import conftest as owned


@pytest_asyncio.fixture(autouse=True)
async def bypass_disposable_role(conn, owned_rls_environment):
    await owned._require_owned_rls_marker(conn, os.environ["CORTEX_OWNED_RLS_RUN_ID"])
    await conn.execute("ALTER ROLE cortex_app_test BYPASSRLS")
    try:
        yield
    finally:
        await conn.execute("ALTER ROLE cortex_app_test NOBYPASSRLS")
