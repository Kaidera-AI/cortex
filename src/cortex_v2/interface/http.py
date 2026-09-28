"""HTTP/OpenAPI presentation generated from the operation registry (R23).

One registry → one generated HTTP contract. Operations of unavailable
modules are absent from the document; they surface through capability
discovery with an explicit reason instead of pretending readiness.
"""

from __future__ import annotations

from typing import Any

from .registry import NormalizedOperation, OperationRegistry

OPENAPI_VERSION = "3.1.0"

def request_json_schema(model: type | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_json_schema()


def _header_parameter(name: str, required: bool) -> dict[str, Any]:
    return {
        "name": name,
        "in": "header",
        "required": required,
        "schema": {"type": "string"},
    }


def _query_parameters(operation: NormalizedOperation) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    if operation.request_model is not None:
        schema = operation.request_model.model_json_schema()
        required = set(schema.get("required", ()))
        for name, property_schema in sorted(
            schema.get("properties", {}).items()
        ):
            parameters.append(
                {
                    "name": name,
                    "in": "query",
                    "required": name in required,
                    "schema": _simplify(property_schema),
                }
            )
    for parameter in operation.query_params:
        parameters.append(
            {
                "name": parameter["name"],
                "in": "query",
                "required": bool(parameter.get("required", False)),
                "schema": parameter.get("schema", {"type": "string"}),
            }
        )
    return parameters


def _simplify(property_schema: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in property_schema:
        variants = [
            variant
            for variant in property_schema["anyOf"]
            if variant.get("type") != "null"
        ]
        if len(variants) == 1:
            merged = dict(variants[0])
            for key, value in property_schema.items():
                if key != "anyOf":
                    merged.setdefault(key, value)
            return merged
    return dict(property_schema)


def _path_parameters(operation: NormalizedOperation) -> list[dict[str, Any]]:
    return [
        {"name": name, "in": "path", "required": True,
         "schema": {"type": "string"}}
        for name in operation.path_params
    ]


def _description(operation: NormalizedOperation) -> str:
    usage = operation.usage
    parts = [operation.summary, "", f"Purpose: {usage['purpose']}"]
    parts.append(f"Use when: {usage['use_when']}")
    parts.append(f"Avoid when: {usage['avoid_when']}")
    parts.append(f"Cost class: {usage['cost_class']}")
    parts.append(f"Evidence contract: {usage['evidence_contract']}")
    return "\n".join(part for part in parts if part)


def operation_openapi(
    operation: NormalizedOperation,
) -> tuple[str, str, dict[str, Any]]:
    parameters = _path_parameters(operation)
    if operation.requires_scope:
        parameters.append(_header_parameter("X-Cortex-Scope", True))
    if operation.kind == "scoped_read":
        parameters.append(_header_parameter("X-Cortex-Read-Scopes", False))
    if operation.requires_idempotency_key:
        parameters.append(_header_parameter("Idempotency-Key", True))
    document: dict[str, Any] = {
        "operationId": operation.operation_id,
        "summary": operation.summary,
        "description": _description(operation),
        "tags": [operation.module],
        "responses": {
            "2XX": {
                "description": "Typed success envelope.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Envelope"}
                    }
                },
            },
            "default": {
                "description": "Typed problem (ApiProblem).",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ApiProblem"}
                    }
                },
            },
        },
    }
    if parameters:
        document["parameters"] = parameters
    if operation.method == "POST":
        if operation.request_model is not None:
            model_name = operation.request_model.__name__
            document["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": f"#/components/schemas/{model_name}"
                        }
                    }
                },
            }
    else:
        document["parameters"] = parameters + _query_parameters(operation)
    return operation.path, operation.method.lower(), document


def _component_schemas(registry: OperationRegistry) -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "ApiProblem": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "retryable": {"type": "boolean"},
                "fields": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            "required": ["code", "message", "retryable"],
        },
        "Envelope": {
            "type": "object",
            "properties": {
                "data": {"type": "object"},
                "request_id": {"type": "string"},
                "contract_version": {"type": "string"},
            },
            "required": ["data", "request_id"],
        },
    }
    for operation in registry.operations_sorted():
        model = operation.request_model
        if model is None or model.__name__ in schemas:
            continue
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", None)
        schemas[model.__name__] = schema
        for name, definition in (definitions or {}).items():
            schemas.setdefault(name, definition)
    return schemas


def openapi_document(
    registry: OperationRegistry,
    *,
    title: str = "Cortex v2",
    product_version: str,
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for operation in registry.operations_sorted():
        if registry.module_state(operation.module).state != "ready":
            continue
        path, method, document = operation_openapi(operation)
        paths.setdefault(path, {})[method] = document
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": title,
            "version": product_version,
            "description": (
                "Generated from the one versioned Cortex v2 operation "
                f"registry ({registry.version}, digest {registry.digest}). "
                "CLI help and MCP tool schemas derive from the same "
                "registry."
            ),
        },
        "paths": {path: paths[path] for path in sorted(paths)},
        "components": {
            "schemas": _component_schemas(registry),
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "v2 principal bearer credential; per-installation, "
                        "never a shared administrator credential."
                    ),
                }
            },
        },
        "security": [{"bearerAuth": []}],
    }
