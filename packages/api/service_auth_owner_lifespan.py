"""Unselected lifecycle composition; importing this module never imports API main.

Strict API integration must explicitly select this adapter and provide the
canonical transactional resolver. No second pool, listener process or issuer.
"""
from contextlib import asynccontextmanager
from service_auth_owner import OwnerServer


def owner_lifespan(original_lifespan, directory, store_provider, resolve_identity):
    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app) as state:
            async with OwnerServer(directory, store_provider(), resolve_identity):
                yield state
    return lifespan
