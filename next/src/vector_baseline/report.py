"""Per-offer diagnostic accounting. Source execution never selects the product engine."""
from collections import defaultdict
import math


def nearest_rank(values, percentile):
    if (not values or not 0 < percentile <= 1
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)):
        raise ValueError("finite observations and percentile required")
    return sorted(values)[math.ceil(len(values) * percentile) - 1]


def summarize(run, *, latency_budget_ms, bindings):
    if (isinstance(latency_budget_ms, bool) or not isinstance(latency_budget_ms, (float, int))
            or not math.isfinite(latency_budget_ms) or latency_budget_ms <= 0):
        raise ValueError("explicit positive latency budget required")
    config, rows = run["config"], run["records"]
    duration = config["duration_seconds"]
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("positive scheduled duration required")
    expected = round(duration * 40)
    valid = (len(rows) == expected and {r["offer_id"] for r in rows} == set(range(expected))
             and config["rps"] == 40 and config["clients"] == 8
             and run["clients_opened"] == run["clients_closed"] == 8 and not run["close_errors"]
             and all(r["phase"] == "measured" and r["client"] == r["offer_id"] % 8
                     and math.isclose(r["scheduled_offset_seconds"], r["offer_id"] / 40, abs_tol=1e-8)
                     and r["status"] in {"OK", "ERROR", "TIMEOUT", "MISSED"} for r in rows))
    latencies = [r["latency_seconds"] * 1000 for r in rows]
    if any(not math.isfinite(x) or x < 0 for x in latencies):
        raise ValueError("invalid latency observation")
    p95 = nearest_rank(latencies, .95) if latencies else None
    successes = [r for r in rows if r["status"] == "OK"]
    successful_rps = len(successes) / duration
    errors = len(rows) - len(successes)
    metrics = [r.get("measurement") for r in successes]
    safe = all(isinstance(m, dict) and m.get("safe") is True for m in metrics)
    per_query = defaultdict(list)
    strict = []
    for row in successes:
        m = row.get("measurement", {})
        if m.get("tie_recall") is not None:
            value = m["tie_recall"]
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid recall observation")
            per_query[row["query_id"]].append(value)
            strict.append(m["strict_recall"])
    # Query cycling is repeated evidence, not additional distinct recall queries.
    means = [sum(values) / len(values) for values in per_query.values()]
    mean = sum(means) / len(means) if means else None
    expected_ids = bindings.get("heldout_query_ids")
    coverage_bound = (isinstance(expected_ids, list) and bool(expected_ids)
                      and all(isinstance(q, str) and q for q in expected_ids)
                      and len(set(expected_ids)) == len(expected_ids))
    expected_queries = set(expected_ids) if coverage_bound else set()
    observed_queries = {r["query_id"] for r in successes if isinstance(r.get("measurement"), dict)}
    missing_queries = sorted(expected_queries - observed_queries)
    unexpected_queries = sorted(observed_queries - expected_queries) if coverage_bound else []
    coverage_complete = coverage_bound and not missing_queries and not unexpected_queries
    recall = ("FAIL" if not safe or unexpected_queries else "NOT_RUN" if mean is None or not coverage_complete
              else "PASS" if mean >= .95 else "FAIL")
    throughput = "PASS" if successful_rps >= 38 else "FAIL"
    latency = "PASS" if p95 is not None and p95 <= latency_budget_ms else "FAIL"
    collection_complete = bool(run["observations"]) and not run["observation_errors"]
    failed = (not valid or errors != 0 or not collection_complete
              or any(r["status"] != "OK" for r in run["warmup_records"])
              or throughput == "FAIL" or latency == "FAIL" or recall == "FAIL")
    windows = []
    for minute in range(math.ceil(duration / 60)):
        group = [r for r in rows if minute * 60 <= r["scheduled_offset_seconds"] < (minute + 1) * 60]
        seconds = min(60, duration - minute * 60)
        windows.append({"minute": minute, "seconds": seconds, "offered": len(group),
                        "successful_rps": sum(r["status"] == "OK" for r in group) / seconds,
                        "p95_ms": nearest_rank([r["latency_seconds"] * 1000 for r in group], .95) if group else None})
    return {"schema": "cortex-b02-run-v1", "engine_decision": "UNDECIDED", "gtm_qualified": False,
            "qualification": "SYNTHETIC_SQL_DIAGNOSTIC", "bindings": bindings,
            "not_run": ["real-Marlow-geometry", "both-native-editions", "verified-OS-cold", "full-GTM-matrix",
                        "API/auth/provider/cache/hybrid", "whole-first-release-stack", "Vera/Kai-acceptance"],
            "diagnostic": {"accounting_valid": valid, "offered": len(rows), "expected_offered": expected,
                           "successful_responses": len(successes), "successful_rps": successful_rps,
                           "errors_or_missed": errors, "throughput_status": throughput, "p95_ms": p95,
                           "latency_budget_ms": latency_budget_ms, "latency_status": latency,
                           "mean_tie_recall": mean, "mean_strict_recall": sum(strict) / len(strict) if strict else None,
                           "distinct_nonempty_queries": len(per_query), "recall_status": recall,
                           "heldout_coverage_complete": coverage_complete,
                           "missing_heldout_queries": missing_queries, "unexpected_query_ids": unexpected_queries,
                           "failed_queries": sorted(q for q, values in per_query.items() if min(values) < .95),
                           "resource_collection_complete": collection_complete, "minute_windows": windows,
                           "warmup_offers": len(run["warmup_records"]),
                           "warmup_errors": sum(r["status"] != "OK" for r in run["warmup_records"]),
                           "verdict": "FAIL" if failed else "NOT_RUN" if recall == "NOT_RUN" else "PASS"},
            "run": run}
