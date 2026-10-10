"""Telemetry semantic probes, using the existing bound runner/shared classifier."""
import mutate_conductor as proof

SOURCE = proof.ROOT / "src/cortex_core/gateway/health_metrics.py"
PREFIX = "test_health_metrics.TelemetryTests."
MUTATIONS = [
    (SOURCE, "early_health_timeout_misclassified",
     'return self._health_error("timeout" if budget.expired() or time.monotonic() >= deadline else "core_unavailable")',
     'return self._health_error("timeout")',
     "test_dependency_timeout.DependencyTimeoutTests.test_immediate_health_timeout_is_dependency_unavailable"),
    (SOURCE, "early_sample_timeout_misclassified",
     'return self._metrics_error("timeout" if budget.expired() or time.monotonic() >= deadline else "sample_unavailable")',
     'return self._metrics_error("timeout")',
     "test_dependency_timeout.DependencyTimeoutTests.test_immediate_sample_timeout_is_sample_unavailable"),
    (SOURCE, "wildcard_binding_admitted", "if not loopback(host):", "if False:",
     PREFIX + "test_wildcard_binding_is_refused_at_construction"),
    (SOURCE, "external_peer_admitted",
     'if scope["type"] == "http" and not (peer and loopback(peer[0])):',
     'if False:', PREFIX + "test_external_scrape_is_refused_before_core_readers"),
    (SOURCE, "reconfigured_wildcard_admitted", "validate_binding(self.host, self.port)",
     'pass', PREFIX + "test_reconfigured_wildcard_is_refused_before_server_call"),
    (SOURCE, "proxy_headers_trusted", 'proxy_headers=False, forwarded_allow_ips=""',
     'proxy_headers=True, forwarded_allow_ips=""',
     PREFIX + "test_binding_is_literal_loopback_with_proxy_trust_disabled"),
    (SOURCE, "health_details_exposed",
     'return {"component": "cortex", "status": ("ok" if state == "healthy" else\n'
     '                "degraded" if available else "unavailable"), "core_available": available,\n'
     '                "conductor": state, "reason": None if available else "core_unavailable"}',
     "return value",
     PREFIX + "test_health_is_minimal_typed_sanitized_and_not_cached"),
    (SOURCE, "health_boolean_coerced",
     'available, state = value.get("core_available"), value.get("state")',
     'available, state = bool(value.get("core_available")), value.get("state")',
     PREFIX + "test_malformed_health_receipts_cannot_claim_healthy"),
    (SOURCE, "core_outage_metrics_served", 'if not health["core_available"]:',
     "if False:", PREFIX + "test_core_outage_reports_health_and_refuses_stale_metrics"),
    (SOURCE, "conductor_outage_hidden", '"degraded" if available else "unavailable"',
     '"ok" if available else "unavailable"',
     PREFIX + "test_conductor_down_is_degraded_while_core_metrics_remain_available"),
    (SOURCE, "noop_sample_serves_cached_metrics",
     'if type(sample) is not CoreSample:\n                    raise ValueError("Invalid Core metric receipt")',
     'if type(sample) is not CoreSample:\n                    return Response(self.metrics.export(), media_type="text/plain")',
     PREFIX + "test_missing_core_sample_refuses_cached_export"),
    (SOURCE, "fabricated_core_sample", "self.metrics.update_core(sample.db_bytes, sample.embed_backlog)",
     "self.metrics.update_core(0, 0)",
     PREFIX + "test_every_scrape_updates_actual_supplied_core_values"),
    (SOURCE, "whole_scrape_timeout_disabled",
     'async def _metrics(self, request):\n        deadline = time.monotonic() + self.timeout_seconds\n'
     '        budget = asyncio.timeout(self.timeout_seconds)',
     'async def _metrics(self, request):\n        deadline = time.monotonic() + self.timeout_seconds\n'
     '        budget = asyncio.timeout(10)',
     PREFIX + "test_stalled_sampler_is_cancelled_within_request_budget"),
    (SOURCE, "late_callback_admitted", "if time.monotonic() >= deadline:", "if False:",
     PREFIX + "test_late_callback_cannot_return_success_after_deadline"),
    (SOURCE, "late_export_admitted", "text = self.metrics.export()\n                self._deadline(deadline)",
     "text = self.metrics.export()", PREFIX + "test_late_metric_serialization_cannot_return_success"),
    (SOURCE, "health_exception_exposed",
     'except Exception:\n            return self._health_error("core_unavailable")',
     'except Exception as error:\n            return self._health_error(str(error))',
     PREFIX + "test_dependency_exceptions_are_scrubbed_before_json"),
    (SOURCE, "sample_exception_exposed",
     'except Exception:\n            return self._metrics_error("sample_unavailable")',
     'except Exception as error:\n            return self._metrics_error(str(error))',
     PREFIX + "test_dependency_exceptions_are_scrubbed_before_json"),
    (SOURCE, "caller_cancellation_swallowed",
     'except Exception:\n            return self._metrics_error("sample_unavailable")',
     'except BaseException:\n            return self._metrics_error("sample_unavailable")',
     PREFIX + "test_cancellation_propagates_and_fake_scraper_closes"),
    (SOURCE, "post_invokes_readers", 'methods=["GET"]', 'methods=["GET", "POST"]',
     PREFIX + "test_post_cannot_invoke_read_ports"),
]


def run():
    proof.MUTATIONS = MUTATIONS
    proof.run()


if __name__ == "__main__":
    run()
