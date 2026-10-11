"""Source-only soak qualification controls with fake clock/workers/evidence."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

SOAK = Path(__file__).resolve().parents[2] / "cli" / "cortex-search-soak"


def source_case(monkeypatch, *, mode="complete", duration=600, fault=None):
    loader = SourceFileLoader("synthetic_soak_completion", str(SOAK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    engine_calls = []
    monkeypatch.setattr(sys, "argv", ["cortex-search-soak", "--project", "synthetic-soak",
                                      "--duration", str(duration), "--concurrency", "1",
                                      "--abort-workers", "1", "--url", "http://127.0.0.1:1",
                                      "--pg-container", "synthetic-pg", "--api-container", "synthetic-api"])

    def engine(*args):
        engine_calls.append(args)
        return "podman"

    monkeypatch.setattr(module, "resolve_container_engine", engine)
    monkeypatch.setattr(module, "admin_stats", lambda *_: {})
    monkeypatch.setattr(module, "container_logs", lambda *_: None if fault == "logs" else "")
    monkeypatch.setattr(module, "normal_worker", lambda *_: None)
    monkeypatch.setattr(module, "abort_worker", lambda *_: None)
    calls = []

    def scans(_):
        calls.append(1)
        return {"idx_decisions_summary_trgm": 1 if fault == "scans" else len(calls)}

    monkeypatch.setattr(module, "trgm_scans", scans)

    class Stats:
        def __init__(self):
            self.lock = threading.Lock()
            self.latencies = [6 if fault == "latency" else .1]
            self.codes = {401 if fault == "edge" else 200: 1}
            self.shed = 0
            self.hard_5xx = 1 if fault == "hard5xx" else 0
            self.errors = {}
            self.aborts = 0 if fault == "abort" else 1
            self.degraded = {}

    monkeypatch.setattr(module, "Stats", Stats)

    class Clock:
        reads = 0
        value = 0.0

        def now(self):
            self.reads += 1
            if mode == "complete":
                return 0.0 if self.reads == 1 else float(duration)
            return self.value

        def sleep(self, _):
            self.value = float(duration) if mode == "late-interrupt" else 0.0
            raise KeyboardInterrupt

    clock = Clock()
    clock_module = SimpleNamespace(**vars(time))
    clock_module.monotonic = clock.now
    clock_module.sleep = clock.sleep
    monkeypatch.setattr(module, "time", clock_module)
    return module, engine_calls


def test_clean_completed_600_second_control_still_passes(monkeypatch, capsys):
    module, _ = source_case(monkeypatch)
    assert module.main() == 0
    assert "GATE PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["interrupt", "late-interrupt"])
def test_interrupt_never_qualifies_or_prints_pass(monkeypatch, capsys, mode):
    module, _ = source_case(monkeypatch, mode=mode)
    status = module.main()
    output = capsys.readouterr().out
    assert status != 0, "interrupted soak qualified clean metrics"
    assert "GATE PASSED" not in output
    assert "interrupted" in output
    assert "== RESULTS ==" in output


def test_short_requested_duration_refuses_without_engine_or_pass(monkeypatch, capsys):
    module, engine_calls = source_case(monkeypatch, duration=599)
    status = module.main()
    output = capsys.readouterr()
    assert status != 0, "short soak qualified"
    assert "GATE PASSED" not in output.out
    assert engine_calls == []
    assert "600s gate minimum" in output.err


@pytest.mark.parametrize("fault", ["edge", "logs", "abort", "hard5xx", "latency", "scans"])
def test_existing_failure_and_inconclusive_guards_remain_nonzero(monkeypatch, capsys, fault):
    module, _ = source_case(monkeypatch, fault=fault)
    assert module.main() != 0
    assert "GATE PASSED" not in capsys.readouterr().out
