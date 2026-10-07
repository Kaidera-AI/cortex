"""Actual sourced operator wrapper, credential-read spy; no token/network."""
import os
from pathlib import Path
import subprocess

import pytest


HELPER = Path(__file__).resolve().parents[2] / "cli/_cortex_api.sh"
SCRIPT = r'''
source "$1"
capture="$2"
cortex_service_auth_token() {
    printf '%s:%s\n' "$1" "$2" >> "$capture"
    return 1
}
if cortex_api_call_admin GET /projects "" "$3"; then
    exit 90
else
    exit 0
fi
'''


@pytest.mark.parametrize("shell", ("/bin/bash", "/bin/zsh"))
@pytest.mark.parametrize("chosen,explicit,expected", (
    ("ops", "", "ops"),
    ("ops", "ops", "ops"),
    (None, "custom", "custom"),
    (None, "", "admin"),
    ("ops", "other", None),
    ("", "", None),
    ("ops@other-project", "", None),
    ("ops extra", "", None),
))
def test_chosen_operator_is_exact_and_never_ambient_or_key_fallback(tmp_path, shell, chosen, explicit, expected):
    config = tmp_path / "empty-config"
    config.mkdir(mode=0o700)
    capture = tmp_path / "PUBLIC-selected-identity.txt"
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CORTEX_", "KAIDERA_", "OPENKAI_", "HARNESS_", "BASH_FUNC_"))
                   and k not in {"DATABASE_URL", "PGPASSWORD", "PGPASSFILE", "BASH_ENV", "ENV"}}
    environment.update(CORTEX_PROJECT="notes", CORTEX_AGENT="worker", CORTEX_AGENT_ID="other-worker",
        CORTEX_AGENTS_DIR=str(config), CORTEX_RUNTIME_CONFIG=str(config / "absent.yaml"),
        CORTEX_WORKSPACE_CONFIG=str(config / "absent.json"), CORTEX_AUTH_TOKEN_DIR=str(config),
        CORTEX_API_URL="https://trusted.invalid")
    if chosen is not None:
        environment["CORTEX_OPERATOR_AGENT"] = chosen
    result = subprocess.run([shell, "-c", SCRIPT, "r409-fixture", str(HELPER), str(capture), explicit],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    calls = capture.read_text().splitlines() if capture.exists() else []
    assert calls == ([] if expected is None else ["notes:" + expected]), "R409 wrong operator selection"
    if expected is None:
        assert "operator" in result.stderr.lower() and "identity" in result.stderr.lower()
