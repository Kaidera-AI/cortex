"""Named client mutants; reuse the bound parent driver and shared classifier."""
import mutate_conductor as proof

SOURCE = proof.ROOT / "src/cortex_core/cli/status.py"
PREFIX = "test_cli_status.CliStatusTests."
MUTATIONS = [
    (SOURCE, "external_origin_admitted", "if not address.is_loopback:", "if False:",
     PREFIX + "test_external_and_dns_endpoints_refused_before_transport"),
    (SOURCE, "nonroot_path_admitted", 'parts.path not in {"", "/"}', "False",
     PREFIX + "test_endpoint_paths_credentials_queries_and_bad_ports_are_refused"),
    (SOURCE, "missing_endpoint_guessed", "return endpoint_url(endpoint), json_mode",
     'return endpoint_url(endpoint or "http://127.0.0.1:17891"), json_mode',
     PREFIX + "test_missing_endpoint_and_unknown_command_refuse_without_default_guess"),
    (SOURCE, "degraded_success", 'code = {"ok": 0, "degraded": 1, "unavailable": 2}',
     'code = {"ok": 0, "degraded": 0, "unavailable": 2}',
     PREFIX + "test_degraded_is_distinct_from_healthy_success"),
    (SOURCE, "unavailable_receipt_lost", "health = await read_health(url, transport)",
     'health = await read_health(url, transport)\n            if health["status"] == "unavailable":\n                raise ValueError()',
     PREFIX + "test_core_unavailable_preserves_server_receipt"),
    (SOURCE, "human_core_outage_fabricated", '"available" if health["core_available"] else "unavailable"',
     '"unavailable" if health["core_available"] else "unavailable"',
     PREFIX + "test_human_output_reports_core_and_conductor_without_readiness"),
    (SOURCE, "http_state_mismatch_admitted",
     '(http_status, value["core_available"], value["conductor"]) != expected[:3]', "False",
     PREFIX + "test_response_http_and_state_must_agree"),
    (SOURCE, "extra_fields_admitted", "set(value) != fields", "not fields.issubset(value)",
     PREFIX + "test_extra_fields_are_refused_without_echoing_private_payload"),
    (SOURCE, "boolean_coercion_admitted", 'type(value["core_available"]) is not bool', "False",
     PREFIX + "test_strict_boolean_and_closed_reason_are_required"),
    (SOURCE, "duplicate_json_key_admitted", "if key in value:", "if False:",
     PREFIX + "test_invalid_json_duplicate_keys_and_wrong_content_type_are_refused"),
    (SOURCE, "wrong_content_type_admitted",
     'response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"', "False",
     PREFIX + "test_invalid_json_duplicate_keys_and_wrong_content_type_are_refused"),
    (SOURCE, "redirect_followed", "follow_redirects=False", "follow_redirects=True",
     PREFIX + "test_redirect_is_not_followed_even_to_another_valid_health_reply"),
    (SOURCE, "environment_proxy_trusted", "trust_env=False", "trust_env=True",
     PREFIX + "test_environment_proxy_cannot_replace_local_health_transport"),
    (SOURCE, "connection_retried", 'error = "connection_unavailable"',
     'try:\n                health = await read_health(url, transport)\n            except Exception:\n                error = "connection_unavailable"',
     PREFIX + "test_connection_failure_is_scrubbed_and_never_retried"),
    (SOURCE, "exception_text_exposed",
     'except Exception:\n            # Never forward dependency exception text or fabricate server health.\n            error = "connection_unavailable"',
     'except Exception as failure:\n            error = str(failure)',
     PREFIX + "test_connection_failure_is_scrubbed_and_never_retried"),
    (SOURCE, "body_cap_raised", "MAX_BODY_BYTES = 16 * 1024", "MAX_BODY_BYTES = 32 * 1024",
     PREFIX + "test_oversized_stream_is_stopped_and_closed"),
    (SOURCE, "content_length_trusted", "len(body) + len(chunk) > MAX_BODY_BYTES",
     'int(response.headers.get("content-length", "0")) > MAX_BODY_BYTES',
     PREFIX + "test_false_content_length_does_not_bypass_actual_body_cap"),
    (SOURCE, "whole_timeout_extended", "asyncio.timeout(BUDGET_SECONDS)", "asyncio.timeout(3)",
     PREFIX + "test_whole_body_deadline_cancels_stalled_stream"),
    (SOURCE, "late_blocking_body_admitted", "if time.monotonic() >= deadline:", "if False:",
     PREFIX + "test_late_blocking_body_cannot_be_reported_healthy"),
    (SOURCE, "late_parser_admitted",
     'health = validated_receipt(body, response.status_code)\n                check_deadline(deadline)\n        check_deadline(deadline)',
     'health = validated_receipt(body, response.status_code)',
     PREFIX + "test_parse_is_inside_deadline"),
    (SOURCE, "cancellation_swallowed", "except Exception:\n            # Never forward",
     "except BaseException:\n            # Never forward",
     PREFIX + "test_caller_cancellation_propagates_and_closes_response"),
    (SOURCE, "interrupt_wrong_exit", "return 130", "return 64",
     PREFIX + "test_interrupted_main_without_traceback"),
    (SOURCE, "server_timeout_reason_lost", 'return {key: value[key] for key in',
     'value["reason"] = "core_unavailable" if value["reason"] == "timeout" else value["reason"]\n    return {key: value[key] for key in',
     PREFIX + "test_server_timeout_reason_is_preserved"),
    (SOURCE, "compressed_body_admitted",
     'response.headers.get("content-encoding", "identity").lower() != "identity"', "False",
     PREFIX + "test_valid_gzip_health_is_refused_before_decompression"),
    (SOURCE, "tls_verification_disabled", "verify=True", "verify=False",
     PREFIX + "test_default_https_transport_keeps_certificate_verification"),
]


def run():
    proof.MUTATIONS = MUTATIONS
    proof.run()


if __name__ == "__main__":
    run()
