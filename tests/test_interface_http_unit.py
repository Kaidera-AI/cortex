"""Unit tests for HTTP/OpenAPI schema generation from the registry (R23)."""

from __future__ import annotations

import json

from cortex_v2.interface.http import (
    openapi_document,
    operation_openapi,
    request_json_schema,
)
from cortex_v2.interface.registry import build_registry
from cortex_v2.models import CreateMemoryRecord


def isolated_registry():
    """Registry with only W1 + own modules; live peer packages excluded."""
    import importlib

    def importer(name):
        if name.startswith(("cortex_v2.interface", "cortex_v2.ingest")):
            return importlib.import_module(name)
        raise ImportError(name)

    return build_registry(importer=importer)


def test_request_schema_from_pydantic_model():
    schema = request_json_schema(CreateMemoryRecord)
    assert schema["type"] == "object"
    assert "record_type" in schema["properties"]
    assert schema["additionalProperties"] is False


def test_request_schema_none_for_modelless_operation():
    assert request_json_schema(None) is None


def test_operation_openapi_post_has_body_and_headers():
    registry = build_registry()
    path, method, document = operation_openapi(registry.get("memory.record"))
    assert path == "/v1/memory/records"
    assert method == "post"
    assert document["operationId"] == "memory.record"
    parameters = {p["name"] for p in document["parameters"]}
    assert "X-Cortex-Scope" in parameters
    assert "Idempotency-Key" in parameters
    assert "requestBody" in document


def test_operation_openapi_get_uses_query_parameters():
    registry = build_registry()
    _, method, document = operation_openapi(registry.get("content.inspect"))
    assert method == "get"
    names = {p["name"] for p in document["parameters"]}
    assert "content_id" in names
    assert "revision" in names
    assert "history" in names
    assert "requestBody" not in document


def test_openapi_document_covers_registry_and_is_deterministic():
    registry = build_registry()
    document = openapi_document(registry, product_version="0.2.1")
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "0.2.1"
    assert "bearerAuth" in document["components"]["securitySchemes"]
    assert "/v1/context/prepare" in document["paths"]
    assert "/v1/memory/records" in document["paths"]
    operation_ids = {
        operation.get("operationId")
        for item in document["paths"].values()
        for operation in item.values()
    }
    for operation in registry.operations_sorted():
        assert operation.operation_id in operation_ids
    again = openapi_document(build_registry(), product_version="0.2.1")
    assert json.dumps(document, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_unavailable_module_operations_are_absent_from_http_document():
    registry = isolated_registry()
    document = openapi_document(registry, product_version="0.2.1")
    assert registry.module_state("coordination").state == "unavailable"
    assert not any(
        "/v1/coordination" in path for path in document["paths"]
    )


def test_error_contract_is_declared():
    registry = build_registry()
    document = openapi_document(registry, product_version="0.2.1")
    problem = document["components"]["schemas"]["ApiProblem"]
    assert "code" in problem["properties"]
    assert "retryable" in problem["properties"]
    responses = document["paths"]["/v1/memory/records"]["post"]["responses"]
    assert "default" in responses
