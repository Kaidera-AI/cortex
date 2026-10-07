"""Public process liveness never supplies private consumer-use evidence."""
import asyncio
from fastapi import FastAPI
import pytest
from service_auth_http import ServiceAuthMiddleware
from test_service_auth_http import Exchange
from test_service_auth_delivery_r408 import ObservedStore


@pytest.mark.parametrize("credentialed", (False, True))
def test_public_liveness_never_records_consumer_use(credentialed):
    async def scenario():
        store = ObservedStore([])
        app = FastAPI()
        async def live(): return {"status": "ok"}
        app.add_api_route("/health/live", live, methods=["GET"])
        adapter = ServiceAuthMiddleware(app, app.router, lambda: store, transport_is_verified=lambda scope: True)
        response = await Exchange("/health/live", headers=None if credentialed else []).run(adapter)
        assert response.status == 200
        assert store.observations == [] and store.lifecycle_writes == []
        assert bool(store.calls) is credentialed
    asyncio.run(scenario())
