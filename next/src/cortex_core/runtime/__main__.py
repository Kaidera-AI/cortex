"""Closed, nonexecuting runtime entrypoint admission. No automatic migrations."""
from collections.abc import Callable, Mapping, Sequence
import json
import sys

from . import signed_member
from .config import AdmissionPlan, RUNTIME_LOCK_MEMBER, admit_config
from .contracts import MAX_CONFIG_BYTES, load_contracts
from .signed_member import Refusal


def admit_entrypoint(argv: Sequence[str], *, aggregate_id: str, observed_target: str,
                     config_reader: Callable[[str, int], bytes], environ: Mapping[str, str]) -> AdmissionPlan:
    """Validate an explicit invocation, never run its process/listener/DB effects.

    Core vectors are native finite-owner operations, status is the existing CLI;
    this Python entrypoint accepts only RC0's four cortex_core.runtime vectors.
    The installation owner must supply aggregate/target identity, never config.
    """
    member = signed_member.request_member(aggregate_id, RUNTIME_LOCK_MEMBER)
    if any(key.startswith(('CORTEX_', 'PG', 'PYTHON', 'UVICORN_', 'OPENROUTER_')) for key in environ):
        raise Refusal('environment_override')
    if type(argv) not in (list, tuple) or not argv or any(type(arg) is not str for arg in argv):
        raise Refusal('invalid_argv')
    mode = argv[0]
    if mode not in ('api', 'supervisor', 'migrate', 'inspect'):
        raise Refusal('unsupported_mode')
    vector = load_contracts().operations['operation_vectors'][mode]['argv'][5:]
    # The only dynamic CLI atom is migrate's operation UUID. It is cross-bound
    # to the validated config below, never interpreted as a path or import.
    if len(argv) != len(vector) or any(type(expected) is str and actual != expected for actual, expected in zip(argv, vector)):
        raise Refusal('invalid_argv')
    try:
        config_raw = config_reader(vector[2], MAX_CONFIG_BYTES)
    except Exception:
        raise Refusal('config_unavailable') from None
    plan = admit_config(member, config_raw, aggregate_id=aggregate_id, observed_target=observed_target, mode=mode)
    if tuple(argv) != plan.argv[5:]:
        raise Refusal('invalid_argv')
    if mode == 'migrate':
        raise Refusal('required_binding_gap', unit='RC2 finite migration owner adapter')
    return plan


def main() -> None:
    # UNBOUND before argv/env/files. No standalone identity/reader default may be
    # invented before the real R01 + installation-owner composition is accepted.
    signed_member.request_member('', RUNTIME_LOCK_MEMBER)
    raise Refusal('required_binding_gap', unit='RC2 installation owner composition')


if __name__ == '__main__':
    try:
        main()
    except Refusal as refusal:
        sys.stderr.write(json.dumps({'reason': refusal.reason, 'unit': refusal.unit}) + '\n')
        raise SystemExit(2)
