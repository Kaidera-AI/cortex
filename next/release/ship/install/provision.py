"""ship/install provision (J06 installer caller): runs roles.provision_steps inside ONE actual Core bootstrap transaction, the sole executor. It is a thin caller, not a second resource
executor: Core's open_bootstrap_resources(config, custody=, deadline=) opens the bounded resource context, its `.transaction('bootstrap')` owns the one transaction, and every secret is resolved from
the encrypted custody (`custody.resolve(purpose)`) at the moment its step runs and passed as the single BOUND parameter. Nothing here reads a file, an environment variable, a process or a shell.
Order (provision_sequence): pre -> Core migrate (the caller's `migrate`, unchanged) -> post -> [ledger grant, PENDING until accepted] -> Finn's durable seed acknowledgement (`seed`, must return True)
-> close. Any refusal or error stops the sequence; nothing is retried and no credential is defaulted."""
import importlib

try:
    from . import roles as R  # packaged layout: cortex_ship.install
except ImportError:
    import roles as R

LEDGER_ACCEPTED = False  # the schema-USAGE grant is Kai's open consult (fb3b7996-9bb8-4b6b-bfdb-dc7050e501c0): never inferred, never run until this is deliberately set True with that decision
PHASES = ('pre', 'post', 'ledger', 'close')


def _open(config, *, custody, deadline):
    """Core's actual bootstrap opener, imported only when used so the module refuses (not crashes) where Core is absent."""
    try:
        resources = importlib.import_module('cortex_core.runtime.resources')
    except ImportError:
        raise R.RoleRefusal('provision_core_missing')
    return resources.open_bootstrap_resources(config, custody=custody, deadline=deadline)


def _secret(custody, purpose):
    raw = custody.resolve(purpose)
    if type(raw) is not bytes or not raw:
        raise R.RoleRefusal('provision_secret')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise R.RoleRefusal('provision_secret')
    if '\x00' in text:
        raise R.RoleRefusal('provision_secret')
    return text


def run_phase(config, *, custody, deadline, phase, seed_ack=False):
    """Run ONE provisioning phase in ONE bootstrap transaction. Refuses before opening anything for an unknown phase, a pending ledger, a close without the acknowledgement, or a non-seed config."""
    if phase == 'ledger' and not LEDGER_ACCEPTED:
        raise R.RoleRefusal('ledger_not_accepted')
    if getattr(config, 'mode', None) != 'seed':
        raise R.RoleRefusal('provision_config')
    binding = R.bind_logins(dict(config.role_logins), config.vault_root)
    steps = R.provision_steps(binding, phase, seed_ack=seed_ack)  # refuses provision_phase / seed_not_acknowledged
    with _open(config, custody=custody, deadline=deadline) as resources:
        with resources.transaction('bootstrap') as connection:
            for sql, purpose in steps:
                if purpose is None:
                    connection.execute(sql)
                else:
                    connection.execute(sql, (_secret(custody, purpose),))


def provision_sequence(config, *, custody, deadline, migrate, seed):
    """The Day-2 order. `migrate()` is Core's unchanged migrate command (Bob's run command); `seed()` is Finn's durable seed and must return exactly True before the finite logins close."""
    run_phase(config, custody=custody, deadline=deadline, phase='pre')
    migrate()
    run_phase(config, custody=custody, deadline=deadline, phase='post')
    if LEDGER_ACCEPTED:
        run_phase(config, custody=custody, deadline=deadline, phase='ledger')
    if seed() is not True:
        raise R.RoleRefusal('seed_not_acknowledged')
    run_phase(config, custody=custody, deadline=deadline, phase='close', seed_ack=True)
