"""Fixed Option-A driver ownership. No pool, injected production port or defaults."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import math
import os
from pathlib import Path
import signal
import threading
import time
from types import MappingProxyType

from cortex_core.driver_logging import private_driver_logs
from .bootstrap_config import BootstrapConfig
from .credentials import read_private_file
from .custody import CustodyVault
from .signed_member import Refusal

_SEED = ContextVar('cortex_option_a_seed_resources', default=None)
_BIRTH = object()
_SEED_PURPOSES = ('bootstrap', 'installer_enrollment', 'api_request')
_CORE_PURPOSES = ('api_request', 'probe')
_MIGRATE_PURPOSES = ('migrator',)


def remaining_seconds(deadline, *, connecting=False):
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise Refusal('resources_deadline')
    remaining = deadline - time.monotonic()
    # libpq connect_timeout is whole seconds and enforces a two-second minimum.
    # Refuse an undersized remaining budget instead of silently extending it.
    if remaining <= 0 or (connecting and remaining < 2):
        raise Refusal('resources_deadline')
    return remaining


def _driver_connect(**kwargs):
    try:
        import psycopg
        return psycopg.Connection.connect(**kwargs)
    except Refusal:
        raise
    except Exception:
        raise Refusal('resources_unavailable') from None


@contextmanager
def _driver_deadline(deadline):
    """Bound even synchronous DNS/driver work in this single-main-thread owner.

    Never borrow an existing process timer or claim a worker-thread deadline.
    Non-main-thread or timer-sharing compositions refuse explicitly.
    """
    if threading.current_thread() is not threading.main_thread() or any(signal.getitimer(signal.ITIMER_REAL)):
        raise Refusal('resources_timer_unavailable')
    previous=signal.getsignal(signal.SIGALRM)
    def expired(signum,frame):raise Refusal('resources_deadline')
    signal.signal(signal.SIGALRM,expired)
    try:
        signal.setitimer(signal.ITIMER_REAL,remaining_seconds(deadline),0.05)
        yield
        remaining_seconds(deadline)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous)


def _certificate(config):
    path = Path(config.vault_root) / config.database['tls']['root_cert_ref']
    return path, hashlib.sha256(read_private_file(path,limit=65536)).digest()


def _connect(config, custody, purpose, deadline):
    # Caller admits the mode and exact custody object before entering here.
    remaining = remaining_seconds(deadline, connecting=True)
    password = custody.resolve(purpose)
    try:
        password = password.decode('utf-8','strict')
        if not password or '\x00' in password:
            raise ValueError
    except (UnicodeError,ValueError):
        raise Refusal('resources_credential') from None
    certificate, digest = _certificate(config)
    remaining = min(remaining, remaining_seconds(deadline,connecting=True))
    milliseconds = max(1,math.floor(remaining*1000))
    connection = None
    try:
        with private_driver_logs(),_driver_deadline(deadline):
            connection = _driver_connect(host=config.database['host'],port=config.database['port'],
                dbname=config.database['name'],user=config.roles[purpose].login,password=password,
                sslmode='verify-full',sslrootcert=str(certificate),gssencmode='disable',
                autocommit=True,prepare_threshold=None,connect_timeout=min(10,math.floor(remaining)),
                options=f'-c statement_timeout={milliseconds} -c lock_timeout={milliseconds}',
                application_name='cortex-option-a-'+purpose)
    except Refusal:
        if connection is not None:_close({'opened':connection})
        raise
    except Exception:
        if connection is not None:_close({'opened':connection})
        raise Refusal('resources_unavailable') from None
    finally:
        del password
    return connection, digest


class _Resources:
    __slots__ = ('_config','_custody','_deadline','_connections','_closed','_in_transaction','_tainted')

    def __init__(self, config, custody, deadline, connections, *, token):
        if token is not _BIRTH:
            raise Refusal('resources_unbound')
        for key,value in (('_config',config),('_custody',custody),('_deadline',deadline),
                          ('_connections',MappingProxyType(connections)),('_closed',False),('_in_transaction',False),('_tainted',False)):
            object.__setattr__(self,key,value)

    def __setattr__(self,name,value):
        raise AttributeError('fixed private resources')

    def __repr__(self):return '<CoreResources private>'

    @property
    def custody(self):return self._custody

    @property
    def connections(self):return self._connections

    @property
    def role_logins(self):return self._config.role_logins

    @property
    def api_request(self):return self._connections['api_request']

    @property
    def probe(self):return self._connections['probe']

    def check_deadline(self):
        if self._closed:
            raise Refusal('resources_closed')
        if self._tainted:
            raise Refusal('resources_tainted')
        return remaining_seconds(self._deadline)


class _FiniteResources(_Resources):
    __slots__ = ()

    @contextmanager
    def transaction(self,purpose,*,_read_only=False):
        self.check_deadline()
        if type(self) is SeedResources:current_seed_resources(self._config,self._deadline)
        if purpose not in self._connections:raise Refusal('resources_purpose')
        if self._in_transaction:raise Refusal('resources_transaction')
        connection=self._connections[purpose]
        object.__setattr__(self,'_in_transaction',True)
        try:
            with private_driver_logs(),_driver_deadline(self._deadline):
                with connection.transaction():
                    if _read_only:
                        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    milliseconds=str(max(1,math.floor(self.check_deadline()*1000)))
                    connection.execute("SELECT pg_catalog.set_config('statement_timeout',%s,true), "
                        "pg_catalog.set_config('lock_timeout',%s,true)",(milliseconds,milliseconds))
                    yield connection
                    self.check_deadline()
                self.check_deadline()
        except BaseException as error:
            object.__setattr__(self,'_tainted',True)
            if isinstance(error,(Refusal,KeyboardInterrupt,SystemExit)):raise
            raise Refusal('resources_transaction') from None
        finally:object.__setattr__(self,'_in_transaction',False)

    @property
    def bootstrap(self):return self._connections['bootstrap']


class BootstrapResources(_FiniteResources):
    __slots__ = ()
    def __repr__(self):return '<BootstrapResources private>'


class SeedResources(_FiniteResources):
    __slots__ = ()
    def __repr__(self):return '<SeedResources private>'

    def observe_catalog(self):
        from .catalog import observe
        return observe(self)

    @property
    def installer_enrollment(self):return self._connections['installer_enrollment']


class CoreResources(_Resources):
    __slots__ = ()


class MigrationResources(_Resources):
    __slots__ = ()

    def migrate(self):
        # The existing engine owns the top-level transaction. Wrapping it in
        # another transaction would hide its commit boundary behind a savepoint.
        self.check_deadline()
        from cortex_core.schema.migrate import apply_forward_migrations
        try:
            with private_driver_logs(),_driver_deadline(self._deadline):
                applied=apply_forward_migrations(self._connections['migrator'])
                self.check_deadline()
            return applied
        except BaseException as error:
            object.__setattr__(self,'_tainted',True)
            if isinstance(error,(Refusal,KeyboardInterrupt,SystemExit)):raise
            raise Refusal('schema_migration') from None


def current_seed_resources(config, deadline):
    current = _SEED.get()
    if (type(current) is not SeedResources or current._closed
            or current._config is not config or current._deadline != deadline
            or type(deadline) not in (int,float)):
        raise Refusal('resources_unbound')
    current.check_deadline()
    return current


def _close(connections):
    failed = False
    for connection in reversed(list(connections.values())):
        try:
            with private_driver_logs():connection.close()
        except Exception:
            failed = True
    return failed


@contextmanager
def _open(config, *, custody, deadline, mode):
    config_mode='seed' if mode=='bootstrap' else mode
    if type(config) is not BootstrapConfig or config.mode != config_mode:
        raise Refusal('resources_mode')
    if any(name.startswith('PG') and value for name,value in os.environ.items()):
        raise Refusal('resources_environment')
    remaining_seconds(deadline, connecting=True)
    if type(custody) is not CustodyVault or custody._config is not config:
        raise Refusal('resources_custody')
    custody._live()
    if mode in ('seed','bootstrap') and _SEED.get() is not None:
        raise Refusal('resources_bound')
    connections = {};carrier=token=None;failed=False
    try:
        purposes={'bootstrap':('bootstrap',),'seed':_SEED_PURPOSES,'serve':_CORE_PURPOSES,'migrate':_MIGRATE_PURPOSES}[mode]
        for purpose in purposes:
            connection, certificate = _connect(config,custody,purpose,deadline)
            connections[purpose] = connection  # ownership precedes any further check
            remaining_seconds(deadline)
            if _certificate(config)[1] != certificate:
                raise Refusal('resources_changed')
        cls = {'bootstrap':BootstrapResources,'seed':SeedResources,'serve':CoreResources,'migrate':MigrationResources}[mode]
        carrier = cls(config,custody,deadline,connections,token=_BIRTH)
        if mode=='seed':token=_SEED.set(carrier)
        yield carrier
    except BaseException:
        failed=True
        raise
    finally:
        if token is not None:_SEED.reset(token)
        if carrier is not None:object.__setattr__(carrier,'_closed',True)
        cleanup_failed = _close(connections)
        if cleanup_failed and not failed:
            raise Refusal('resources_cleanup')


def open_seed_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='seed')


def open_core_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='serve')


def open_migration_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='migrate')


def open_bootstrap_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='bootstrap')
