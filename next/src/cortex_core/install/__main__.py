"""Nonexecuting shared client. Standalone identity/composition is UNBOUND."""
import json
import sys

from cortex_core.runtime import signed_member
from cortex_core.runtime.config import RUNTIME_LOCK_MEMBER

from .contracts import InstallRefusal, absolute_path
from .planner import PlanResult, dependency, plan


def plan_entrypoint(argv, *, aggregate_id, observed_target, owner_uid, host_identity_ref,
                    environ, request_reader, config_reader):
    try:
        # Exact sole authority seam precedes argv/env/files, as in RC2.
        signed_member.request_member(aggregate_id, RUNTIME_LOCK_MEMBER)
        if type(argv) not in (list, tuple) or len(argv) != 3 or argv[0] != '--request-file' or argv[2] != '--json':
            raise InstallRefusal('bad_shape')
        absolute_path(argv[1])
        if type(environ) is not dict or environ:
            raise InstallRefusal('unsafe_custody')
        try:
            raw = request_reader(argv[1], 1048576)
        except Exception:
            return dependency('original owner request reader')
        return plan(raw, aggregate_id=aggregate_id, observed_target=observed_target, owner_uid=owner_uid,
                    host_identity_ref=host_identity_ref, environ=environ, config_reader=config_reader)
    except signed_member.Refusal as refusal:
        return dependency(signed_member.MISSING_UNIT if refusal.unit == signed_member.MISSING_UNIT else 'runtime ABI owner bindings')
    except InstallRefusal as refusal:
        return PlanResult('FAIL', refusal.reason_code)


def main():
    try:
        signed_member.request_member('', RUNTIME_LOCK_MEMBER)
    except signed_member.Refusal:
        return dependency(signed_member.MISSING_UNIT)
    return dependency('P1 installation owner composition')


if __name__ == '__main__':
    result = main()
    sys.stderr.write(json.dumps({'result': result.result, 'reason_code': result.reason_code, 'unit': result.unit}) + '\n')
    raise SystemExit(2)
