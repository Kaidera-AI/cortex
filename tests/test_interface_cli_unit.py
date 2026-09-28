"""Unit tests for the cortex2 CLI generated from the operation registry (R23).

The CLI is exercised with an injected stub client; no server is contacted.
"""

from __future__ import annotations

import io
import json

from cortex_v2.cli.main import EXIT_API, EXIT_OK, EXIT_TRANSPORT, EXIT_USAGE, main
from cortex_v2.clients.client import CallResult
from cortex_v2.clients.errors import CortexApiError, CortexTransportError
from cortex_v2.interface.registry import build_registry


class StubClient:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error
        self.registry = build_registry()

    def call(self, operation_id, **kwargs):
        self.calls.append((operation_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.result or CallResult(
            operation_id=operation_id,
            status=200,
            data={"ok": True},
            replayed=False,
            request_id="req-1",
        )


def run(argv, client=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, client=client, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_help_lists_registry_operations():
    code, out, _ = run(["--help"])
    assert code == EXIT_OK
    assert "context.prepare" in out
    assert "memory.record" in out
    assert "capability.discover" in out


def test_card_works_offline_from_local_registry():
    code, out, _ = run(["card", "context.prepare"])
    assert code == EXIT_OK
    card = json.loads(out)
    assert card["operation_id"] == "context.prepare"
    assert card["use_when"]
    assert card["avoid_when"]
    assert card["evidence_contract"]


def test_card_unknown_operation_is_usage_error():
    code, _, err = run(["card", "nope.missing"])
    assert code == EXIT_USAGE
    assert "nope.missing" in err


def test_operation_subcommand_sends_payload_and_scope():
    client = StubClient()
    code, out, _ = run(
        [
            "memory.record",
            "--data",
            '{"record_type": "note", "body": "hello"}',
            "--scope",
            "proj-a",
            "--idempotency-key",
            "key-1",
        ],
        client=client,
    )
    assert code == EXIT_OK
    operation_id, kwargs = client.calls[0]
    assert operation_id == "memory.record"
    assert kwargs["payload"] == {"record_type": "note", "body": "hello"}
    assert kwargs["scope"] == "proj-a"
    assert kwargs["idempotency_key"] == "key-1"
    assert json.loads(out)["data"] == {"ok": True}


def test_write_without_idempotency_key_is_usage_error():
    client = StubClient()
    code, _, err = run(
        ["memory.record", "--data", "{}", "--scope", "proj-a"], client=client
    )
    assert code == EXIT_USAGE
    assert "Idempotency-Key" in err or "idempotency" in err.lower()
    assert client.calls == []


def test_path_params_and_query_flags():
    client = StubClient()
    code, _, _ = run(
        [
            "content.inspect",
            "--scope",
            "proj-a",
            "--path-param",
            "content_id=22222222-2222-4222-8222-222222222222",
            "--query",
            "revision=2",
        ],
        client=client,
    )
    assert code == EXIT_OK
    _, kwargs = client.calls[0]
    assert kwargs["path_params"] == {
        "content_id": "22222222-2222-4222-8222-222222222222"
    }
    assert kwargs["query"] == {"revision": "2"}


def test_data_file_and_stdin(tmp_path):
    client = StubClient()
    path = tmp_path / "payload.json"
    path.write_text('{"record_type": "note", "body": "from file"}')
    code, _, _ = run(
        [
            "memory.record",
            "--data-file",
            str(path),
            "--scope",
            "proj-a",
            "--idempotency-key",
            "k",
        ],
        client=client,
    )
    assert code == EXIT_OK
    assert client.calls[0][1]["payload"]["body"] == "from file"


def test_api_error_maps_to_exit_code_and_json():
    client = StubClient(
        error=CortexApiError(
            status=409,
            code="idempotency_key_reused",
            message="Use a new key.",
            retryable=False,
            request_id="r9",
            operation_id="memory.record",
        )
    )
    code, _, err = run(
        ["memory.record", "--data", "{}", "--scope", "s", "--idempotency-key", "k"],
        client=client,
    )
    assert code == EXIT_API
    reported = json.loads(err)
    assert reported["error"]["code"] == "idempotency_key_reused"
    assert reported["error"]["request_id"] == "r9"


def test_transport_error_maps_to_exit_code():
    client = StubClient(error=CortexTransportError("connection refused"))
    code, _, err = run(["protocol.descriptor"], client=client)
    assert code == EXIT_TRANSPORT
    assert "connection refused" in err


def test_capabilities_command_reports_module_states():
    result = CallResult(
        operation_id="capability.discover",
        status=200,
        data={
            "modules": [
                {"module": "interface", "state": "ready"},
                {"module": "coordination", "state": "unavailable",
                 "reason": "module not deployed"},
            ],
            "operations": [],
        },
        replayed=False,
        request_id="r",
    )
    client = StubClient(result=result)
    code, out, _ = run(["capabilities", "--scope", "proj-a"], client=client)
    assert code == EXIT_OK
    assert "coordination" in out
    assert "unavailable" in out
    assert "module not deployed" in out


def test_commands_needing_server_fail_with_usage_error_without_client():
    code, _, err = run(["capabilities", "--scope", "proj-a"])
    assert code == EXIT_USAGE
    assert "config" in err.lower() or "credential" in err.lower()


def test_invalid_json_payload_is_usage_error():
    client = StubClient()
    code, _, err = run(
        ["memory.record", "--data", "{broken", "--scope", "s",
         "--idempotency-key", "k"],
        client=client,
    )
    assert code == EXIT_USAGE
    assert err


def test_module_entrypoint_exists():
    import cortex_v2.cli.__main__ as entry

    assert callable(entry.main)
