"""BODY-checked restored API and public disposable probe-role fixture."""
from cortex_core.api import c11a

PROBE_ROLE = 'kaidera-test-rc1-probe'

def require_probe(case):
    probe = getattr(c11a, 'postgres_core_probe', None)
    factory = getattr(c11a, 'PostgresProbeFactory', None)
    case.assertTrue(callable(probe), 'C11 lacks the database-backed Core probe')
    case.assertTrue(callable(factory), 'C11 lacks the isolated async probe factory')
    return factory, probe

def require_shutdown(case, probe):
    close = getattr(probe, 'aclose', None)
    case.assertTrue(callable(close), 'Core probe lacks an explicit owned shutdown drain')
    return close
