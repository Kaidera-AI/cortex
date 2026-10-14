"""Test-only entry meter for the SAME sealed reader, including every alias.

The earlier module wrapper remains unchanged and is explicitly a direct scope.
No reader or RC2 verification is replaced. Profiling is restored on all exits.
"""
import sys

from cortex_core.runtime import signed_member


def measure(case, *, target, route):
    reader_code = signed_member.read_member.__code__
    events = []
    previous = sys.getprofile()

    def observe(frame, event, arg):
        if previous is not None:
            previous(frame, event, arg)
        if event != 'call' or frame.f_code is not reader_code:
            return
        caller = frame.f_back
        while caller is not None and not caller.f_globals.get('__name__', '').startswith('cortex_core.'):
            caller = caller.f_back
        events.append({
            'aggregate_id': frame.f_locals['aggregate_id'],
            'member_name': frame.f_locals['member_name'],
            'reader_source': frame.f_code.co_filename,
            'reader_first_line': frame.f_code.co_firstlineno,
            'caller_module': caller.f_globals['__name__'] if caller is not None else None,
            'caller_function': caller.f_code.co_name if caller is not None else None,
            'caller_line': caller.f_lineno if caller is not None else None,
        })

    try:
        sys.setprofile(observe)
        result, requests, old_direct_reads, configs = case.select(case.q(target, route), target=target)
    finally:
        sys.setprofile(previous)
    return result, {
        'request_member_count': len(requests),
        'direct_p1_read_count': sum(event['caller_module'].startswith('cortex_core.install.')
                                  for event in events if event['caller_module'] is not None),
        'rc2_config_alias_read_count': sum(event['caller_module'] == 'cortex_core.runtime.config'
                                         for event in events),
        'composed_read_count': len(events),
        'old_module_wrapper_count': len(old_direct_reads),
        'request_member_calls': [list(call) for call in requests],
        'config_reader_calls': [list(call) for call in configs],
        'read_member_events': events,
        'meter_scope': 'actual sealed-reader function-code entries on the single P1 thread; every alias included',
    }
