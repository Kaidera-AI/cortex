"""One failure-safe lifecycle shared by product operations; no actor-granted PASS."""

from .compare import require_independent, compare_archives, compare_closure


def lifecycle(stages, operate, custody, cleanup, record):
    results = {}
    failure = None
    custody_result = None
    cleanup_result = None
    custody_error = None
    cleanup_error = None
    try:
        for stage in stages:
            results[stage] = operate(stage)
            observed = results[stage]
            if isinstance(observed, dict) and "exit" in observed:
                if type(observed["exit"]) is not int or observed["exit"] != 0:
                    raise ValueError("operation failed: " + stage)
    except BaseException as error:
        failure = type(error).__name__
    finally:
        try:
            custody_result = custody()
        except BaseException as error:
            custody_error = type(error).__name__
        finally:
            try:
                cleanup_result = cleanup()
            except BaseException as error:
                cleanup_error = type(error).__name__
    passed = (
        bool(results)
        and failure is None
        and custody_error is None
        and cleanup_error is None
        and custody_result is not None
        and custody_result.get("complete") is True
        and cleanup_result is not None
        and cleanup_result.get("complete") is True
        and cleanup_result.get("remaining_owned_resources") == []
    )
    result = {
        "result": "PASS" if passed else "RED",
        "stages": results,
        "reason": failure or custody_error or cleanup_error,
        "custody": custody_result,
        "cleanup": cleanup_result,
    }
    record(result)
    return result


def verify_pair(a, b):
    require_independent(a["identity"], b["identity"])
    if a["exit"] != 0 or b["exit"] != 0:
        raise ValueError("independent build did not succeed")
    compare_archives(a["archive"], b["archive"])
    compare_closure(a["oci"], b["oci"])
    if a["audit"] != b["audit"] or a["audit"].get("complete") is not True:
        raise ValueError("semantic audit incomplete or differs")
    if a["cleanup"] is not True or b["cleanup"] is not True:
        raise ValueError("build cleanup missing")
    return {
        "raw_archives_equal": True,
        "oci_closure_equal": True,
        "semantic_audit_equal": True,
    }


def plan_source_recipes(raw_envelopes, requests, fact_reader, roots, limits):
    """Nonexecuting recipe SOURCE entry; never the native producer."""
    from .envelopes import plan_source_recipes as plan

    return plan(raw_envelopes, requests, fact_reader, roots, limits)
