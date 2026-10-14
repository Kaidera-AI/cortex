"""Test-only historical module-attribute read meter (known incomplete)."""

def measure(case, *, target, route):
    result, requests, direct_reads, configs = case.select(case.q(target, route), target=target)
    return result, {
        'request_member_count': len(requests),
        'direct_p1_read_count': len(direct_reads),
        'rc2_config_alias_read_count': 0,
        'composed_read_count': len(direct_reads),
        'old_module_wrapper_count': len(direct_reads),
        'request_member_calls': [list(call) for call in requests],
        'config_reader_calls': [list(call) for call in configs],
        'read_member_events': [],
        'meter_scope': 'module attribute only; imported aliases omitted',
    }
