"""Bound-export preflight only: never opens a listener or sends HTTP."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from next.tests.live_contract import contract_gate as gate

BINDING = dict(source_commit="a"*40, version="0.1.004", profile="A",
               listener="http://127.0.0.1:8501")
OPERATIONS = {"POST /records": ["read"], "GET /records/{id}": ["delete"],
              "DELETE /records/{id}": []}


def fixture():
    schema = dict(type="object", properties=dict(id=dict(type="string")), required=["id"])
    def response():
        return dict(description="typed outcome", content={"application/json": {"schema": schema}})
    paths = {}
    for key, links in OPERATIONS.items():
        method, path = key.split(" ")
        op = dict(operationId={"POST": "create", "GET": "read", "DELETE": "delete"}[method],
                  security=[{"token": []}], responses={"200": response(), "400": response()})
        if links:
            op["responses"]["200"]["links"] = {name: dict(operationId=name,
                parameters={"id": "$response.body#/id"}) for name in links}
        if method == "POST":
            op["requestBody"] = dict(required=True, content={"application/json": {"schema": schema}})
        if "{id}" in path:
            op["parameters"] = [dict(name="id", **{"in": "path"}, required=True,
                                    schema={"type": "string"})]
        paths.setdefault(path, {})[method.lower()] = op
    document = dict(openapi="3.1.0", info=dict(title="Bound runtime", version=BINDING["version"]),
        servers=[dict(url=BINDING["listener"])], paths=paths,
        components=dict(securitySchemes=dict(token=dict(type="http", scheme="bearer"))),
        **{"x-cortex-binding": dict(BINDING)})
    return document


def bound(document):
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return raw, dict(BINDING, spec_sha256=hashlib.sha256(raw).hexdigest())


class LiveContract(unittest.TestCase):
    def test_bound_spec_complete(self):
        document = fixture()
        document["info"]["title"] = "runtime DRAFT"
        raw, binding = bound(document)
        with self.assertRaises(gate.ReceiptRefusal) as raised:
            gate.bound_spec_complete(raw, binding, OPERATIONS)
        self.assertEqual(raised.exception.code, "spec_incomplete")

    def test_valid_preflight_has_zero_http_credit(self):
        raw, binding = bound(fixture())
        result = gate.bound_spec_complete(raw, binding, OPERATIONS)
        self.assertEqual(result["operations"], sorted(OPERATIONS))
        self.assertEqual(result["mutating_requests"], 0)
        self.assertEqual(result["qualification"], "preflight_only")

    def test_binding_hash_commit_version_profile_listener_drift(self):
        for field in (*BINDING, "spec_sha256"):
            with self.subTest(field=field):
                raw, binding = bound(fixture())
                binding[field] = "wrong"
                with self.assertRaises(gate.ReceiptRefusal) as raised:
                    gate.bound_spec_complete(raw, binding, OPERATIONS)
                self.assertEqual(raised.exception.code, "spec_binding_mismatch")

    def test_missing_routes_schemas_security_links_and_placeholders(self):
        for defect in ("route", "schema", "security", "link", "request", "ref", "error", "parameter", "link_expression", "link_parameter"):
            with self.subTest(defect=defect):
                doc = fixture()
                create = doc["paths"]["/records"]["post"]
                if defect == "route":
                    del doc["paths"]["/records/{id}"]["delete"]
                elif defect == "schema":
                    create["responses"]["200"]["content"]["application/json"]["schema"] = {}
                elif defect == "security":
                    create["security"] = []
                elif defect == "link":
                    del create["responses"]["200"]["links"]
                elif defect == "request":
                    del create["requestBody"]
                elif defect == "ref":
                    create["responses"]["200"]["content"]["application/json"]["schema"] = {"$ref": "#/missing"}
                elif defect == "link_expression":
                    create["responses"]["200"]["links"]["read"]["parameters"]["id"] = "$response.body#/missing"
                elif defect == "link_parameter":
                    create["responses"]["200"]["links"]["read"]["parameters"] = {"wrong": "$response.body#/id"}
                elif defect == "error":
                    del create["responses"]["400"]
                else:
                    del doc["paths"]["/records/{id}"]["get"]["parameters"]
                raw, binding = bound(doc)
                with self.assertRaises(gate.ReceiptRefusal) as raised:
                    gate.bound_spec_complete(raw, binding, OPERATIONS)
                self.assertEqual(raised.exception.code, "spec_incomplete")

    def test_current_draft_rejected_before_http(self):
        raw = (ROOT / "next/contracts/openapi.json").read_bytes()
        binding = dict(BINDING, spec_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaises(gate.ReceiptRefusal):
            gate.bound_spec_complete(raw, binding, OPERATIONS)

    def test_empty_duplicate_or_nonfinite_json_refuses(self):
        for raw in (b"{}", b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(gate.ReceiptRefusal):
                gate.bound_spec_complete(raw, dict(BINDING,
                    spec_sha256=hashlib.sha256(raw).hexdigest()), OPERATIONS)


if __name__ == "__main__":
    unittest.main()
