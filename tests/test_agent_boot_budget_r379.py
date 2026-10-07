"""Exact frozen legacy budget algorithm; no new renderer used as its own oracle."""
import copy
import hashlib
import importlib.util
import json

import pytest

from boot_r374_fixture import ROOT, required, seed


def legacy_oracle():
    path = ROOT / "legacy-budget-oracle.py"
    provenance = json.loads((ROOT / "legacy-budget-oracle-provenance.json").read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance["oracle_sha256"]
    spec = importlib.util.spec_from_file_location("r379_frozen_legacy_budget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.legacy_budget


@pytest.mark.parametrize("budget", (0, 50, 80, 500, 1200))
@pytest.mark.parametrize("full", (False, True))
def test_token_hint_clamp_optional_tier_order_and_full_flag_preserve_actual_old_algorithm(budget, full):
    module = required()
    snapshot, _ = seed()
    snapshot["operational"]["boot_tail"] = "PUBLIC mandatory operational line."
    tiers = {
        "P1": "--- CRITICAL LESSONS ---\n" + "  ⚑ PUBLIC lesson body\n" * 90,
        "P2": "--- TOP DECISIONS ---\n" + "  * PUBLIC quality body\n" * 40,
        "P3": "--- TOPIC RECALL: PUBLIC ---\n" + "  [PUBLIC] recall body\n" * 20,
    }
    snapshot["operational"]["optional_tiers"] = copy.deepcopy(tiers)
    p0 = snapshot["persona_rows"][0]["boot_manifest"]["identity_text"] + "\n" + snapshot["operational"]["boot_tail"]
    expected = legacy_oracle()(p0, tiers["P1"], tiers["P2"], tiers["P3"], budget)
    before = copy.deepcopy(snapshot)
    result = module.project_boot_response(snapshot, budget=budget, query="PUBLIC", full=full)
    assert result["boot"] == expected
    assert result["boot"].startswith(p0) and snapshot == before


def test_budget_oracle_keeps_over_budget_mandatory_tier_and_would_detect_cutting_it():
    # Already-green independent negative-control fixture.
    p0 = "PUBLIC mandatory identity/policy\n" * 100
    result = legacy_oracle()(p0, "PUBLIC optional1" * 50, "PUBLIC optional2" * 50, "PUBLIC optional3" * 50, 50)
    assert result == p0 and result != p0[:200]
