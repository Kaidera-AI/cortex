"""ship/install provision (J06 installer, day 3): the ONE fixed Nix sequence that Core's owner.run imports (Core2 private join). It is a thin caller, not a second executor, resolver or guard:
    provision_sequence(config, *, custody, deadline, operation_key) -> the validated seed receipt
Each stage runs the COMPLETE actual body and its owned resource cleanup inside `cortex_core.runtime.custody._installer_phase(config, custody=, deadline=, phase=)`, in the exact order
PRE, MIGRATE, POST, LEDGER, SEED, ACK, CLOSE, once. Nix owns the SQL and the order; Core owns admission, the stage guard, cleanup and the private ACK:
  PRE     actual open_bootstrap_resources, ONE transaction('bootstrap'); the four bound passwords (migrator, installer_enrollment, api_request, probe) come only from Core's private
          `_installer_password`; public custody.resolve is never called.
  MIGRATE actual open_migration_resources(...).migrate(), unchanged engine, no outer transaction.
  POST    actual bootstrap transaction (set-only membership), LEDGER actual bootstrap transaction (API login core USAGE + core.schema_migrations SELECT only).
  SEED    actual open_seed_resources and the UNCHANGED seed_runtime_bindings(config, operation_key, deadline).
  ACK     after every seed resource closed: operator._reload_seed_receipt(config, custody=, operation_key=, deadline=, result=); Core records the validated receipt privately.
  CLOSE   actual bootstrap transaction; the finite-login close SQL is rendered with seed_ack=True ONLY here, inside the admitted CLOSE guard, never from a caller value.
There is no migrate/seed callback, no caller acknowledgement, no default and no fallback import: a missing Core name is the typed non-success `provision_core_missing`. Any failure propagates
unchanged (the guard taints the owner) and nothing later starts. Nothing here reads a file, an environment variable, a process or a shell."""
import importlib

try:
    from . import roles as R  # packaged layout: cortex_ship.install
except ImportError:
    import roles as R

LEDGER_ACCEPTED = True  # Kai approved Option A (consult fb3b7996-9bb8-4b6b-bfdb-dc7050e501c0): ONLY the exact configured API login gains core USAGE + SELECT on core.schema_migrations; no new PUBLIC/group/probe grant.
ORDER = ('PRE', 'MIGRATE', 'POST', 'LEDGER', 'SEED', 'ACK', 'CLOSE')
FOUR = ('migrator', 'installer_enrollment', 'api_request', 'probe')
_SQL = {'PRE': 'pre', 'POST': 'post', 'LEDGER': 'ledger', 'CLOSE': 'close'}


class _Core:
    """Core's private join, resolved when used (never at import) so the module refuses instead of crashing where Core2 is absent."""

    _NAMES = (('cortex_core.runtime.custody', '_installer_phase'), ('cortex_core.runtime.custody', '_installer_password'),
              ('cortex_core.runtime.resources', 'open_bootstrap_resources'), ('cortex_core.runtime.resources', 'open_migration_resources'),
              ('cortex_core.runtime.resources', 'open_seed_resources'), ('cortex_core.runtime.operator', '_reload_seed_receipt'),
              ('cortex_core.schema.seed', 'seed_runtime_bindings'))

    def __init__(self):
        try:
            self._modules = {name: importlib.import_module(name) for name, _ in self._NAMES}
        except ImportError:
            raise R.RoleRefusal('provision_core_missing') from None
        if not all(callable(getattr(self._modules[module], attr, None)) for module, attr in self._NAMES):
            raise R.RoleRefusal('provision_core_missing')

    def __getattr__(self, attr):
        for module, name in self._NAMES:
            if name == attr:
                return getattr(self._modules[module], name)
        raise AttributeError(attr)


def _text(raw):
    if type(raw) is not bytes or not raw:
        raise R.RoleRefusal('provision_secret')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise R.RoleRefusal('provision_secret')
    if '\x00' in text:
        raise R.RoleRefusal('provision_secret')
    return text


def _admit(config, operation_key):
    """Pure checks before anything is imported or opened."""
    if getattr(config, 'mode', None) != 'install':
        raise R.RoleRefusal('provision_config')
    if type(operation_key) is not str or not operation_key:
        raise R.RoleRefusal('provision_operation_key')
    return R.bind_logins(dict(config.role_logins), config.vault_root)


def _sql_stage(core, binding, config, custody, deadline, phase):
    if phase == 'LEDGER' and not LEDGER_ACCEPTED:
        raise R.RoleRefusal('ledger_not_accepted')   # a guard turned off is a loud refusal, never a silent omission
    with core._installer_phase(config, custody=custody, deadline=deadline, phase=phase):
        steps = R.provision_steps(binding, _SQL[phase], seed_ack=True if phase == 'CLOSE' else False)   # True exists only inside the admitted CLOSE guard
        with core.open_bootstrap_resources(config, custody=custody, deadline=deadline) as resources:
            with resources.transaction('bootstrap') as connection:
                for sql, purpose in steps:
                    if purpose is None:
                        connection.execute(sql)
                    else:
                        connection.execute(sql, (_text(core._installer_password(config, custody=custody, deadline=deadline, purpose=purpose)),))


def run_phase(config, *, custody, deadline, phase, operation_key=None):
    """One SQL/MIGRATE stage on its own, kept only so a direct, early, repeated or foreign call is denied by Core's guard BEFORE any connector or password. It takes no acknowledgement and does
    not run SEED or ACK, which exist only inside provision_sequence."""
    binding = _admit(config, operation_key or 'direct')
    if phase not in ORDER or phase in ('SEED', 'ACK'):
        raise R.RoleRefusal('provision_phase')
    core = _Core()
    if phase == 'MIGRATE':
        with core._installer_phase(config, custody=custody, deadline=deadline, phase=phase):
            with core.open_migration_resources(config, custody=custody, deadline=deadline) as resources:
                resources.migrate()
        return None
    return _sql_stage(core, binding, config, custody, deadline, phase)


def provision_sequence(config, *, custody, deadline, operation_key):
    binding = _admit(config, operation_key)
    core = _Core()
    for phase in ('PRE', 'MIGRATE', 'POST', 'LEDGER'):
        if phase == 'MIGRATE':
            run_phase(config, custody=custody, deadline=deadline, phase=phase, operation_key=operation_key)
        else:
            _sql_stage(core, binding, config, custody, deadline, phase)
    with core._installer_phase(config, custody=custody, deadline=deadline, phase='SEED'):
        with core.open_seed_resources(config, custody=custody, deadline=deadline):
            result = core.seed_runtime_bindings(config, operation_key, deadline)
    with core._installer_phase(config, custody=custody, deadline=deadline, phase='ACK'):
        receipt = core._reload_seed_receipt(config, custody=custody, operation_key=operation_key, deadline=deadline, result=result)
    _sql_stage(core, binding, config, custody, deadline, 'CLOSE')
    return receipt
