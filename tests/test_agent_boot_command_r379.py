"""Actual member transport/CLI, unissued reader, qualified public body fixtures."""
import copy
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cortex_v2.cli import agent_request as bridge
from test_member_client_integration import FixtureReader, member_profile
from test_client_transport_origin import isolated_transport_environment  # noqa: F401
from boot_r374_fixture import CASES, ROOT, install_public_bodies, json_bytes, native_expected, packet, seed


def required():
    assert importlib.util.find_spec("cortex_v2.cli.agent_boot") is not None, "R379 native boot command missing"
    module = importlib.import_module("cortex_v2.cli.agent_boot")
    assert callable(getattr(module, "run", None)), "R379 native boot command missing"
    return module


def wire(tmp_path, response, agent="kai", status=200):
    records = []
    reader = FixtureReader(project="helix")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            records.append({"method": "GET", "path": self.path,
                "scope": self.headers.get("X-Cortex-Scope"), "agent": self.headers.get("X-Agent-Name"),
                "version": 1 if self.headers.get("Authorization") == "Bearer " + "A" * 43 else 0,
                "body_length": int(self.headers.get("Content-Length", 0)),
                "idempotency": self.headers.get("Idempotency-Key"),
                "read_scopes": self.headers.get("X-Cortex-Read-Scopes")})
            body = response if isinstance(response, bytes) else json_bytes(response)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = member_profile(f"http://127.0.0.1:{server.server_port}", reader)
    profile.default_scope = "helix"
    profile.principal_label = agent
    profile.workspace_root = str(tmp_path / "permanent-code-root")
    return profile, reader, records, server, thread


def invoke(profile, argv, monkeypatch):
    monkeypatch.setattr(bridge, "load_member_profile", lambda *a: profile)
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(["boot", *argv], stdin=io.StringIO(), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("agent,variant,options", CASES)
def test_nine_native_commands_keep_exact_ratified_payload_and_current_url_semantics(agent, variant, options, tmp_path, monkeypatch):
    required()
    root = tmp_path / "permanent-code-root"
    root.mkdir(mode=0o700)
    snapshot, original = seed(agent, variant, root)
    install_public_bodies(snapshot, root)
    expected = native_expected(snapshot, original)
    profile, reader, records, server, thread = wire(tmp_path, expected, agent)
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    try:
        code, out, err = invoke(profile, [agent.upper() + "@helix:PUBLIC", *options], monkeypatch)
        assert code == 0 and err == "" and out.encode() == json_bytes(expected)
        budget = options[options.index("--budget") + 1] if "--budget" in options else "1200"
        path = "/boot/" + agent + "?budget=" + budget
        if "--full" in options:
            path += "&full=true"
        if "--query" in options:
            path += "&query=release"
        assert records == [{"method": "GET", "path": path, "scope": "helix", "agent": agent,
            "version": 1, "body_length": 0, "idempotency": None, "read_scopes": None}]
        assert reader.reads == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("defect", ("missing-file", "changed-file", "same-bytes-symlink", "parent-symlink", "wrong-root", "missing-proof", "wrong-actor", "wrong-project", "duplicate-pin", "bad-digest", "duplicate-json-key", "nonfinite-json", "denied"))
def test_root_body_proof_response_failures_never_print_partial_success_or_retry(defect, tmp_path, monkeypatch):
    required()
    root = tmp_path / "permanent-code-root"
    root.mkdir(mode=0o700)
    snapshot, original = seed(workspace_root=root)
    install_public_bodies(snapshot, root)
    response = native_expected(snapshot, original)
    first = snapshot["skill_rows"][0]
    target = root / first["boot_manifest"]["body_ref"]
    if defect == "missing-file":
        target.unlink()
    elif defect == "changed-file":
        target.write_text("PUBLIC changed local bytes; server digest is unchanged")
    elif defect == "same-bytes-symlink":
        outside = tmp_path / "PUBLIC-other-body"
        outside.write_text(first["body"])
        target.unlink(); target.symlink_to(outside)
    elif defect == "parent-symlink":
        outside = tmp_path / "PUBLIC-other-directory"
        target.parent.rename(outside)
        target.parent.symlink_to(outside, target_is_directory=True)
    elif defect == "wrong-root":
        response["persona"]["metadata"]["boot_validation"]["workspace_root"] = str(tmp_path / "foreign-root")
    elif defect == "missing-proof":
        response["persona"]["metadata"].pop("boot_validation")
    elif defect == "wrong-actor":
        response["persona"]["agent"] = "bob"
    elif defect == "wrong-project":
        response["persona"]["project"] = "PUBLIC-other-project"
    elif defect == "duplicate-pin":
        response["persona"]["metadata"]["boot_validation"]["body_pins"].append(copy.deepcopy(response["persona"]["metadata"]["boot_validation"]["body_pins"][0]))
    elif defect == "bad-digest":
        response["persona"]["metadata"]["boot_validation"]["body_pins"][0]["body_sha256"] = "0" * 64
    elif defect == "duplicate-json-key":
        response = b'{"boot":"PUBLIC","boot":"PUBLIC changed","persona":{},"surface_version":"PUBLIC"}'
    elif defect == "nonfinite-json":
        response = b'{"boot":"PUBLIC","persona":{},"surface_version":"PUBLIC","untrusted":NaN}'
    profile, reader, records, server, thread = wire(tmp_path, response, status=403 if defect == "denied" else 200)
    try:
        code, out, err = invoke(profile, ["kai"], monkeypatch)
        assert code != 0 and out == "" and err and "Bearer" not in err and "Traceback" not in err
        assert reader.reads == len(records) == 1
        if defect in ("missing-file", "changed-file", "same-bytes-symlink", "parent-symlink"):
            assert first["slug"] in err  # Named missing/changed/escaped body refusal.
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("args", ([], ["--help"], ["-h"]))
def test_current_boot_help_is_profile_and_credential_free(args, monkeypatch):
    required()
    def forbidden(*a):
        pytest.fail("help must not load any profile or credential")
    monkeypatch.setattr(bridge, "load_member_profile", forbidden)
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(["boot", *args], stdin=io.StringIO(), stdout=out, stderr=err)
    expected = "Usage: cortex-boot <agent> [--budget N] [--query <text>] [--full]\n\nPrints boot context for an agent in the current Cortex project.\n"
    assert code == 0 and out.getvalue() == expected and err.getvalue() == ""


def test_release_boot_wrapper_forwards_original_options_and_nonsecret_profile(tmp_path):
    required()
    wrapper = Path(__file__).resolve().parents[1] / "scripts/agent-shims/cortex-boot"
    directory = tmp_path / "bin"
    directory.mkdir()
    shutil.copyfile(wrapper, directory / "cortex-boot")
    (directory / "cortex-agent").write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    (directory / "cortex-agent").chmod(0o755)
    result = subprocess.run(["bash", str(directory / "cortex-boot"), "KAI@helix", "--query", "PUBLIC / é+?", "--full"],
        env=dict(os.environ, CORTEX_CONNECTION_PROFILE="/PUBLIC/non-secret-profile.json"), capture_output=True, text=True)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == ["--config", "/PUBLIC/non-secret-profile.json", "boot", "KAI@helix", "--query", "PUBLIC / é+?", "--full"]


def test_raw_agent_bridge_boot_mapping_uses_same_qualified_output_and_member(tmp_path, monkeypatch):
    required()
    root = tmp_path / "permanent-code-root"
    root.mkdir(mode=0o700)
    snapshot, original = seed(workspace_root=root)
    install_public_bodies(snapshot, root)
    expected = native_expected(snapshot, original)
    profile, reader, records, server, thread = wire(tmp_path, expected)
    monkeypatch.setattr(bridge, "load_member_profile", lambda *a: profile)
    try:
        out, err = io.StringIO(), io.StringIO()
        code = bridge.main(["api", "GET", "/boot/kai?budget=50&full=true", "--agent-name", "kai"],
                           stdin=io.StringIO(), stdout=out, stderr=err)
        assert code == 0 and err.getvalue() == "" and out.getvalue().encode() == json_bytes(expected)
        assert len(records) == reader.reads == 1 and records[0]["agent"] == "kai"
        assert records[0]["path"] == "/boot/kai?budget=50&full=true" and records[0]["method"] == "GET"
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("argv", (("api", "POST", "/boot/kai"),
                                   ("api", "GET", "https://unissued-marker@foreign.example/boot/kai"),
                                   ("api", "GET", "/boot/kai?unmapped=PRIVATE-unissued-marker")))
def test_unmapped_boot_method_origin_or_option_refuses_before_profile_or_key(argv, monkeypatch):
    def forbidden(*a): pytest.fail("unsafe/unmapped request loaded a profile")
    monkeypatch.setattr(bridge, "load_member_profile", forbidden)
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(list(argv), stdin=io.StringIO(), stdout=out, stderr=err)
    assert code != 0 and out.getvalue() == "" and "unissued-marker" not in err.getvalue()
    assert "Bearer" not in err.getvalue() and "foreign.example" not in err.getvalue()


@pytest.mark.parametrize("agent,variant,options", CASES)
def test_frozen_actual_legacy_command_emits_each_original_packet_verbatim(agent, variant, options, tmp_path):
    # Already-green legacy oracle control; no native module is substituted.
    directory = tmp_path / "legacy"
    directory.mkdir()
    shutil.copyfile(ROOT / "cortex-boot", directory / "cortex-boot")
    (directory / "_cortex_api.sh").write_text(
        'cortex_agent_base_name() { local base="${1%%@*}"; printf "%s" "${base%%:*}"; }\n'
        'cortex_api_urlencode() { python3 -S -c "import sys;from urllib.parse import quote;print(quote(sys.argv[1]),end=\\\"\\\")" "$1"; }\n'
        'cortex_api_call() { [ "$1" = GET ] || return 99; printf "%s" "$2" >"$PUBLIC_CALL"; cat "$PUBLIC_RESPONSE"; }\n'
    )
    path = ROOT / (agent + "-" + variant + ".json")
    env = dict(os.environ, PUBLIC_RESPONSE=str(path), PUBLIC_CALL=str(directory / "call"))
    env.pop("CORTEX_BOOT_BUDGET", None)
    result = subprocess.run(["bash", str(directory / "cortex-boot"), agent.upper() + "@helix:PUBLIC", *options], env=env, capture_output=True)
    assert result.returncode == 0 and result.stderr == b"" and result.stdout == path.read_bytes()
    assert (directory / "call").read_text().startswith("/boot/" + agent + "?budget=")
