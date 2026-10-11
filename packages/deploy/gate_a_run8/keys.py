"""Public console host-key classification; only zero-key absence is retryable."""
from __future__ import annotations

import base64
import hashlib
import importlib
from pathlib import Path
import re

from custody import Refusal, require


SHARED_SHA = "3ff62b762e6a3274e7c58394121e312519dc2f37716fd64ec5e17a9969fd4d67"


def shared_classifier():
    source = Path(__file__).with_name("console_keys.py")
    require(hashlib.sha256(source.read_bytes()).hexdigest() == SHARED_SHA, "shared_classifier_source_drift")
    module = importlib.import_module("console_keys")
    require(Path(module.__file__).resolve() == source.resolve(), "shared_classifier_module_alias")
    return module


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
    outcome = shared_classifier().classify_console_keys(keys)
    if outcome == {"status": "WAIT", "exit": 75}:
        return {"state": "WAIT_NO_CONSOLE_ED25519", "exit": 75, "retryable": True, "count": 0}
    if outcome == {"status": "CONFLICT", "exit": 76}:
        return {"state": "STOP_CONFLICTING_CONSOLE_ED25519", "exit": 76, "retryable": False,
                "count": len(set(keys))}
    require(outcome.get("status") == "ONE" and outcome.get("exit") == 0
            and outcome.get("key") in keys, "unclassified_console_outcome")
    return {"state": "ONE_CONSOLE_ED25519", "exit": 0, "retryable": False, "count": 1,
            "key": outcome["key"]}


def classify_decoded(text):
    # Keep donor line 67 extraction, then call the shared cardinality function.
    keys = re.findall(r"^ssh-ed25519\s+([A-Za-z0-9+/=]+)(?:\s|$)", text, flags=re.M)
    return classify(keys)


def decoded_outcome(text, representation="donor-auto"):
    return classify_decoded(decode_console(text, representation))


def bind_console(invoke, retire, clock, sleep, *, watchdog_armed):
    require(watchdog_armed is True, "key_wait_watchdog_missing")
    def attempt():
        try:
            return decoded_outcome(invoke())
        except Refusal as error:
            if error.code != "STOP_CONSOLE_DECODE_FAILURE":
                raise
            return {"state": "DECODE_STOP", "exit": 76, "retryable": False}
    return shared_classifier().bind_with_wait(attempt, retire, clock, sleep)


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
