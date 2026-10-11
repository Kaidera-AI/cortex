"""Public console host-key classification; only zero-key absence is retryable."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re

from custody import Refusal, require


CARDINALITY_SHA = "225e51e8947b51693ad43915aff770d8455bf06dc9e3090bfe34f5fe8727fa4a"
ZERO_REASON = "fresh AWS console ED25519 host key not available; no trust-on-first-use"
CONFLICT_REASON = "multiple distinct AWS console ED25519 host keys; permanent identity refusal"


def decode_console(text, representation="donor-auto"):
    require(isinstance(text, str) and len(text.encode()) <= 1024 * 1024, "console_output_shape")
    require(representation in {"decoded", "base64", "donor-auto"}, "console_representation")
    if representation == "base64" or (representation == "donor-auto" and "ssh-ed25519 " not in text):
        try:
            text = base64.b64decode(text, validate=True).decode()
        except (ValueError, UnicodeError) as error:
            raise Refusal("STOP_CONSOLE_DECODE_FAILURE") from error
    return text


def console_keys(text, representation="donor-auto"):
    text = decode_console(text, representation)
    return re.findall(r"^ssh-ed25519\s+([A-Za-z0-9+/=]+)(?:\s|$)", text, re.M)


def classify(keys):
    require(isinstance(keys, list) and all(isinstance(k, str) and k for k in keys), "console_keys_shape")
    # Extraction and cardinality have ONE source: C's frozen 67-72 block. This
    # adapter translates its public refusal into GateA's typed caller contract.
    return classify_decoded("\n".join("ssh-ed25519 " + key for key in keys))


def classify_decoded(text):
    source = Path(__file__).with_name("templates") / "console-cardinality.inc"
    body = source.read_bytes()
    require(hashlib.sha256(body).hexdigest() == CARDINALITY_SHA, "shared_classifier_source_drift")
    scope = {"text": text, "re": re}
    try:
        exec(compile(body, str(source), "exec"), scope)
    except SystemExit as error:
        reason = str(error.code)
        if reason == ZERO_REASON:
            return {"state": "WAIT_NO_CONSOLE_ED25519", "exit": 75, "retryable": True, "count": 0}
        require(reason == CONFLICT_REASON, "unrecognised_cardinality_refusal")
        return {"state": "STOP_CONFLICTING_CONSOLE_ED25519", "exit": 76, "retryable": False,
                "count": len(set(scope["keys"]))}
    return {"state": "ONE_CONSOLE_ED25519", "exit": 0, "retryable": False, "count": 1,
            "key": scope["expected"]}


def decoded_outcome(text, representation="donor-auto"):
    return classify_decoded(decode_console(text, representation))


def scan_binding(console, scan_text, expected_host, representation="donor-auto"):
    outcome = decoded_outcome(console, representation)
    require(outcome["exit"] == 0, outcome["state"])
    rows = [line for line in scan_text.splitlines() if line and not line.startswith("#")]
    require(len(rows) == 1, "scan_key_count")
    parts = rows[0].split()
    require(len(parts) >= 3 and parts[:2] == [expected_host, "ssh-ed25519"]
            and parts[2] == outcome["key"], "host_key_mismatch")
    return {"state": "BOUND", "distinct_console_keys": 1, "scan_matches_console": True}


class KeyWait:
    def __init__(self, started):
        self.started = started
        self.last_attempt = None

    def admit_attempt(self, now, watchdog_armed):
        require(watchdog_armed is True, "key_wait_watchdog_missing")
        require(0 <= now - self.started < 600, "key_wait_deadline")
        require(self.last_attempt is None or now - self.last_attempt >= 60, "key_wait_too_frequent")
        self.last_attempt = now

    def retry(self, outcome, now):
        require(outcome == {"state": "WAIT_NO_CONSOLE_ED25519", "exit": 75,
                            "retryable": True, "count": 0}, "key_failure_not_retryable")
        require(0 <= now - self.started < 600, "key_wait_deadline")
        return 60
