"""Bound OpenAPI metadata preflight; no HTTP, request generation or release credit."""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from next.tests.ci_gates.receipt import ReceiptRefusal, decode, digest, read_json, require

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
BINDING_FIELDS = {"source_commit", "version", "profile", "listener"}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _ref(document, ref):
    require(type(ref) is str and ref.startswith("#/"), "spec_incomplete")
    value = document
    try:
        for token in ref[2:].split("/"):
            value = value[token.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        raise ReceiptRefusal("spec_incomplete") from None
    return value


def _concrete(schema, document, seen=()):
    """Reject absent/unfrozen shapes; this is not a second JSON Schema validator.

    Full schema semantics and generated HTTP remain the existing schema library /
    Schemathesis job in day 2. This narrow guard accepts only explicitly typed
    shapes or concrete local references/compositions; unknown shapes fail closed.
    """
    require(type(schema) is dict and bool(schema), "spec_incomplete")
    if "$ref" in schema:
        ref = schema["$ref"]
        require(ref not in seen, "spec_incomplete")
        return _concrete(_ref(document, ref), document, (*seen, ref))
    for name in ("oneOf", "anyOf", "allOf"):
        if name in schema:
            require(type(schema[name]) is list and bool(schema[name]), "spec_incomplete")
            for child in schema[name]:
                _concrete(child, document, seen)
            return
    kind = schema.get("type")
    require(type(kind) is str and kind in
            {"object", "array", "string", "integer", "number", "boolean"}, "spec_incomplete")
    if kind == "object":
        properties = schema.get("properties")
        require(type(properties) is dict and bool(properties), "spec_incomplete")
        for child in properties.values():
            _concrete(child, document, seen)
        required = schema.get("required", [])
        require(type(required) is list and all(type(key) is str and key in properties
                                              for key in required), "spec_incomplete")
    if kind == "array":
        _concrete(schema.get("items"), document, seen)


def _content(value, document):
    require(type(value) is dict and type(value.get("content")) is dict
        and bool(value["content"]), "spec_incomplete")
    for media, item in value["content"].items():
        require(type(media) is str and "/" in media and type(item) is dict, "spec_incomplete")
        _concrete(item.get("schema"), document)


def bound_spec_complete(spec, binding, published_operations):
    """Reject incomplete or wrongly bound raw export before any network operation.

    published_operations maps independent 'METHOD /path' keys to required standard
    OpenAPI Link names. The caller owns actual runtime route inventory/auth truth.
    A positive result qualifies metadata only, never installed behavior.
    """
    try:
        require(type(spec) is bytes and 0 < len(spec) <= 8*1024*1024, "spec_incomplete")
        try:
            document = decode(spec)
        except ReceiptRefusal:
            raise ReceiptRefusal("spec_incomplete") from None
        require(type(document) is dict and document.get("openapi") == "3.1.0", "spec_incomplete")
        # Current C01 DRAFT is a deliberate rejection fixture.
        text = json.dumps(document).lower()
        require(not any(word in text for word in ("draft", "unfrozen", "placeholder")),
                "spec_incomplete")
        require(type(binding) is dict and set(binding) == BINDING_FIELDS | {"spec_sha256"},
                "spec_binding_mismatch")
        require(all(type(binding[key]) is str and bool(binding[key]) for key in binding),
                "spec_binding_mismatch")
        require(re.fullmatch("[0-9a-f]{40}", binding["source_commit"]) is not None
            and digest(spec) == binding["spec_sha256"], "spec_binding_mismatch")
        require(document.get("x-cortex-binding") ==
            {key: binding[key] for key in BINDING_FIELDS}, "spec_binding_mismatch")
        require(document.get("info", {}).get("version") == binding["version"]
            and document.get("servers") == [{"url": binding["listener"]}], "spec_binding_mismatch")
        url = urlsplit(binding["listener"])
        require(url.scheme in {"http", "https"} and bool(url.hostname) and url.port is not None
            and not url.username and not url.password and not url.query and not url.fragment
            and url.path in ("", "/"), "spec_binding_mismatch")
        require(type(published_operations) is dict and bool(published_operations)
            and all(type(key) is str and type(links) is list
                and all(type(link) is str and bool(link) for link in links)
                and len(links) == len(set(links))
                for key, links in published_operations.items()), "spec_incomplete")
        paths = document.get("paths")
        require(type(paths) is dict and bool(paths), "spec_incomplete")
        operations = {}
        for path, item in paths.items():
            require(type(path) is str and path.startswith("/") and type(item) is dict,
                    "spec_incomplete")
            for method in METHODS & set(item):
                require(type(item[method]) is dict, "spec_incomplete")
                operations[method.upper() + " " + path] = (path, method, item, item[method])
        require(set(operations) == set(published_operations), "spec_incomplete")
        ids = [op.get("operationId") for _, _, _, op in operations.values()]
        by_id = {op.get("operationId"): (path, method, item, op)
                 for path, method, item, op in operations.values()
                 if type(op.get("operationId")) is str}
        require(all(type(value) is str and value for value in ids)
            and len(ids) == len(set(ids)), "spec_incomplete")
        schemes = document.get("components", {}).get("securitySchemes", {})
        for node in _walk(document):
            if "$ref" in node:
                _ref(document, node["$ref"])
            if "schema" in node:
                _concrete(node["schema"], document)
        for key, (path, method, item, op) in operations.items():
            security = op.get("security", document.get("security"))
            require(type(security) is list and bool(security), "spec_incomplete")
            for alternative in security:
                require(type(alternative) is dict and bool(alternative), "spec_incomplete")
                for name, scopes in alternative.items():
                    require(name in schemes and type(scopes) is list
                        and all(type(scope) is str for scope in scopes), "spec_incomplete")
                    scheme = schemes[name]
                    require(type(scheme) is dict and scheme.get("type") in
                            {"http", "apiKey", "oauth2", "openIdConnect"}, "spec_incomplete")
                    if scheme["type"] == "http":
                        require(bool(scheme.get("scheme")), "spec_incomplete")
                    elif scheme["type"] == "apiKey":
                        require(bool(scheme.get("name")) and scheme.get("in") in
                                {"header", "query", "cookie"}, "spec_incomplete")
                    elif scheme["type"] == "oauth2":
                        declared = {scope for flow in scheme.get("flows", {}).values()
                                    for scope in flow.get("scopes", {})}
                        require(bool(scheme.get("flows")) and set(scopes) <= declared, "spec_incomplete")
                    else:
                        require(bool(scheme.get("openIdConnectUrl")), "spec_incomplete")
            parameters = item.get("parameters", []) + op.get("parameters", [])
            require(type(parameters) is list, "spec_incomplete")
            for name in re.findall(r"{([^{}]+)}", path):
                matches = [param for param in parameters if type(param) is dict
                           and param.get("in") == "path" and param.get("name") == name]
                require(len(matches) == 1 and matches[0].get("required") is True, "spec_incomplete")
                _concrete(matches[0].get("schema"), document)
            if method in {"post", "put", "patch"}:
                request = op.get("requestBody")
                require(type(request) is dict and request.get("required") is True, "spec_incomplete")
                _content(request, document)
            responses = op.get("responses")
            require(type(responses) is dict and bool(responses), "spec_incomplete")
            success = [value for status, value in responses.items()
                       if type(status) is str and re.fullmatch("2[0-9]{2}", status)]
            errors = [value for status, value in responses.items()
                      if type(status) is str and re.fullmatch("[45][0-9]{2}", status)]
            require(bool(success) and bool(errors), "spec_incomplete")
            for response in (*success, *errors):
                _content(response, document)
            link_names = set()
            owned_links = []
            for response in success:
                links = response.get("links", {})
                require(type(links) is dict and all(type(name) is str and bool(name)
                                                   for name in links), "spec_incomplete")
                link_names.update(links)
                owned_links.extend((response, link) for link in links.values())
            require(set(published_operations[key]) <= link_names, "spec_incomplete")
            for response, link in owned_links:
                require(type(link) is dict and link.get("operationId") in ids
                    and type(link.get("parameters")) is dict and bool(link["parameters"]),
                    "spec_incomplete")
                target_path = by_id[link["operationId"]][0]
                require(set(re.findall(r"{([^{}]+)}", target_path)) == set(link["parameters"]),
                        "spec_incomplete")
                for expression in link["parameters"].values():
                    require(type(expression) is str and expression.startswith("$response.body#/"),
                            "spec_incomplete")
                    fields = expression.removeprefix("$response.body#/").split("/")
                    require(len(fields) == 1, "spec_incomplete")
                    field = fields[0].replace("~1", "/").replace("~0", "~")
                    # Each actual media alternative of this containing response
                    # must supply the field. Another status/media cannot lend it.
                    shapes = [media["schema"] for media in response["content"].values()]
                    shapes = [_ref(document, value["$ref"]) if "$ref" in value else value
                              for value in shapes]
                    require(all(field in shape.get("required", [])
                                and field in shape.get("properties", {}) for shape in shapes),
                            "spec_incomplete")
        return dict(operations=sorted(operations), spec_sha256=digest(spec),
                    mutating_requests=0, qualification="preflight_only")
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise ReceiptRefusal("spec_incomplete") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("binding", type=Path)
    parser.add_argument("operations", type=Path)
    args = parser.parse_args()
    try:
        result = bound_spec_complete(args.spec.read_bytes(), read_json(args.binding),
                                     read_json(args.operations))
        print(json.dumps(result, sort_keys=True))
        return 0
    except ReceiptRefusal as error:
        print(json.dumps(dict(refusal=error.code, mutating_requests=0)))
        return 1
    except (KeyError, TypeError, OSError):
        print(json.dumps(dict(refusal="spec_incomplete", mutating_requests=0)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
