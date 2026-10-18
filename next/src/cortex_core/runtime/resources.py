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
from .custody import CustodyVault, _current_installer, _installer_connect_password, _installer_fail
from .signed_member import Refusal

_SEED = ContextVar('cortex_option_a_seed_resources', default=None)
_BIRTH = object()
_SEED_PURPOSES = ('bootstrap', 'installer_enrollment', 'api_request')
_CORE_PURPOSES = ('api_request', 'probe')
_MIGRATE_PURPOSES = ('migrator',)


class ShutdownBudget:
    """One config-bound reducing cap, including connections not yet yielded."""
    __slots__=('_config','_deadline')

    def __init__(self,config):
        if type(config) is not BootstrapConfig or config.mode!='serve':raise Refusal('resources_mode')
        self._config=config;self._deadline=None

    @property
    def deadline(self):return self._deadline

    def begin(self,deadline=None):
        if self._deadline is None:
            now=time.monotonic();cap=now+self._config.limits['shutdown_seconds']
            if deadline is not None:
                if type(deadline) not in (int,float) or not now<deadline<=cap:raise Refusal('resources_shutdown')
                cap=deadline
            self._deadline=cap
        return self._deadline


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


def _connect(config, custody, purpose, deadline,*,_shutdown=None):
    # Caller admits the mode and exact custody object before entering here.
    remaining = remaining_seconds(deadline, connecting=True)
    password = (_installer_connect_password(config, custody, deadline, purpose)
                if config.mode == 'install' else custody.resolve(purpose))
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
        if connection is not None:_close({'opened':connection},deadline=_shutdown.deadline if _shutdown is not None else None)
        raise
    except Exception:
        if connection is not None:_close({'opened':connection},deadline=_shutdown.deadline if _shutdown is not None else None)
        raise Refusal('resources_unavailable') from None
    except BaseException:
        if connection is not None:_close({'opened':connection},deadline=_shutdown.deadline if _shutdown is not None else None)
        raise
    finally:
        del password
    return connection, digest


class _Resources:
    __slots__ = ('_config','_custody','_deadline','_connections','_closed','_in_transaction','_tainted','_shutdown_deadline')

    def __init__(self, config, custody, deadline, connections, *, token):
        if token is not _BIRTH:
            raise Refusal('resources_unbound')
        for key,value in (('_config',config),('_custody',custody),('_deadline',deadline),
                          ('_connections',MappingProxyType(connections)),('_closed',False),('_in_transaction',False),('_tainted',False),('_shutdown_deadline',None)):
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

    def _live(self):
        if self._closed:
            raise Refusal('resources_closed')
        if self._tainted:
            raise Refusal('resources_tainted')
        if self._config.mode == 'install':
            owner = _installer_resource_owner(self._config, self._custody, self._deadline,
                        {BootstrapResources:'bootstrap', SeedResources:'seed',
                         MigrationResources:'migrate'}.get(type(self)))
            if owner._resources is not self:
                raise Refusal('resources_unbound')

    def check_deadline(self):
        self._live()
        return remaining_seconds(self._deadline)


class _FiniteResources(_Resources):
    __slots__ = ()

    @contextmanager
    def transaction(self,purpose,*,_read_only=False,_repeatable=True):
        self.check_deadline()
        if self._shutdown_deadline is not None:raise Refusal('resources_draining')
        if type(self) is SeedResources:current_seed_resources(self._config,self._deadline)
        if purpose not in self._connections:raise Refusal('resources_purpose')
        if self._in_transaction:raise Refusal('resources_transaction')
        connection=self._connections[purpose]
        object.__setattr__(self,'_in_transaction',True)
        try:
            with private_driver_logs(),_driver_deadline(self._deadline):
                with connection.transaction():
                    if _read_only:
                        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
                            if _repeatable else 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY')
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

    def catalog(self):
        from .catalog import validated_seed
        return validated_seed(self)

    @property
    def installer_enrollment(self):return self._connections['installer_enrollment']


class CoreResources(_FiniteResources):
    __slots__ = ()

    def begin_shutdown(self,deadline):
        try:
            if remaining_seconds(deadline)>self._config.limits['shutdown_seconds']:
                raise Refusal('resources_shutdown')
        except Refusal:raise Refusal('resources_shutdown') from None
        current=self._shutdown_deadline
        object.__setattr__(self,'_shutdown_deadline',deadline if current is None else min(current,deadline))

    def observe_service_catalog(self):
        from .catalog import observe_service
        return observe_service(self)

    @contextmanager
    def request_connection(self,purpose,*,deadline):
        """Reuse the two owned sessions under one caller's absolute request cap.

        C04 owns its top-level transaction and cleanup. There is no enclosing
        transaction/savepoint, executor, reconnect, or automatic write retry.
        """
        self._live()
        if self._shutdown_deadline is not None:raise Refusal('resources_draining')
        if purpose not in _CORE_PURPOSES:raise Refusal('resources_purpose')
        if remaining_seconds(deadline)>min(10,self._config.limits['request_seconds']):
            raise Refusal('resources_deadline')
        if self._in_transaction:raise Refusal('resources_transaction')
        from cortex_core.auth import AuthError
        from cortex_core.records import RecordError
        connection=self._connections[purpose]
        def idle():
            from psycopg.pq import TransactionStatus
            try:
                return (not connection.closed and connection.autocommit is True
                    and connection.info.transaction_status==TransactionStatus.IDLE)
            except Exception:return False
        object.__setattr__(self,'_in_transaction',True)
        try:
            with private_driver_logs(),_driver_deadline(deadline):
                if not idle():raise Refusal('resources_request')
                milliseconds=str(max(1,math.floor(remaining_seconds(deadline)*1000)))
                connection.execute("SELECT pg_catalog.set_config('statement_timeout',%s,false), "
                    "pg_catalog.set_config('lock_timeout',%s,false)",(milliseconds,milliseconds))
                yield connection
                remaining_seconds(deadline)
                if not idle():raise Refusal('resources_request')
        except BaseException as error:
            if isinstance(error,(AuthError,RecordError)) and error.code in {
                    'unauthenticated','forbidden','scope_mismatch','invalid_input','conflict','gone'}:
                try:remaining_seconds(deadline);reusable=idle()
                except Refusal:reusable=False
                if reusable:raise
            object.__setattr__(self,'_tainted',True)
            if isinstance(error,(Refusal,KeyboardInterrupt,SystemExit)):raise
            raise Refusal('resources_request') from None
        finally:object.__setattr__(self,'_in_transaction',False)


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


def _close(connections,*,deadline=None):
    if deadline is None:deadline=time.monotonic()+1
    failed = False
    for connection in reversed(list(connections.values())):
        try:
            with private_driver_logs(),_driver_deadline(deadline):connection.close()
        except BaseException:
            failed = True
    return failed


def _installer_resource_owner(config, custody, deadline, mode):
    owner = _current_installer(config, custody, deadline)
    stages = {'bootstrap': ('PRE','POST','LEDGER','CLOSE'),
              'migrate': ('MIGRATE',), 'seed': ('SEED',)}
    if not owner._busy or owner._stage not in stages.get(mode, ()):
        if owner._busy: _installer_fail(owner)
        raise Refusal('resources_mode')
    return owner


@contextmanager
def _open(config, *, custody, deadline, mode,_shutdown=None):
    config_mode='seed' if mode=='bootstrap' else mode
    if type(config) is not BootstrapConfig:
        raise Refusal('resources_mode')
    installer = None
    if config.mode == 'install':
        if mode == 'serve': raise Refusal('resources_mode')
        installer = _installer_resource_owner(config, custody, deadline, mode)
        if installer._resources is not None or installer._record_open:
            raise Refusal('resources_bound')
    elif config.mode != config_mode:
        raise Refusal('resources_mode')
    if _shutdown is not None and (type(_shutdown) is not ShutdownBudget or _shutdown._config is not config or mode!='serve'):
        raise Refusal('resources_shutdown')
    if any(name.startswith('PG') and value for name,value in os.environ.items()):
        raise Refusal('resources_environment')
    remaining_seconds(deadline, connecting=True)
    if type(custody) is not CustodyVault or custody._config is not config:
        raise Refusal('resources_custody')
    custody._live()
    if mode in ('seed','bootstrap') and _SEED.get() is not None:
        raise Refusal('resources_bound')
    connections = {};carrier=token=None;failed=False
    if installer is not None: object.__setattr__(installer,'_resources',True)
    try:
        purposes={'bootstrap':('bootstrap',),'seed':_SEED_PURPOSES,'serve':_CORE_PURPOSES,'migrate':_MIGRATE_PURPOSES}[mode]
        for purpose in purposes:
            if _shutdown is not None and _shutdown.deadline is not None:raise Refusal('resources_draining')
            connection, certificate = _connect(config,custody,purpose,deadline,_shutdown=_shutdown)
            connections[purpose] = connection  # ownership precedes any further check
            if _shutdown is not None and _shutdown.deadline is not None:raise Refusal('resources_draining')
            remaining_seconds(deadline)
            if _certificate(config)[1] != certificate:
                raise Refusal('resources_changed')
        cls = {'bootstrap':BootstrapResources,'seed':SeedResources,'serve':CoreResources,'migrate':MigrationResources}[mode]
        carrier = cls(config,custody,deadline,connections,token=_BIRTH)
        if installer is not None: object.__setattr__(installer,'_resources',carrier)
        if mode=='seed':token=_SEED.set(carrier)
        yield carrier
        if installer is not None: carrier.check_deadline()
    except BaseException:
        failed=True
        if installer is not None: _installer_fail(installer)
        raise
    finally:
        reset_failed = False
        if token is not None:
            try: _SEED.reset(token)
            except (ValueError,RuntimeError): reset_failed = True
        if carrier is not None:object.__setattr__(carrier,'_closed',True)
        cleanup_deadline=carrier._shutdown_deadline if carrier is not None else None
        if _shutdown is not None and _shutdown.deadline is not None:
            cleanup_deadline=_shutdown.deadline if cleanup_deadline is None else min(cleanup_deadline,_shutdown.deadline)
        if cleanup_deadline is None:cleanup_deadline=time.monotonic()+config.limits['shutdown_seconds']
        try:
            cleanup_failed = _close(connections,deadline=cleanup_deadline) or reset_failed
        finally:
            if installer is not None: object.__setattr__(installer,'_resources',None)
        if cleanup_failed and installer is not None: _installer_fail(installer)
        if cleanup_failed and (installer is not None or not failed):
            raise Refusal('resources_cleanup')


def open_seed_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='seed')


def open_core_resources(config, *, custody, deadline,_shutdown=None):
    return _open(config,custody=custody,deadline=deadline,mode='serve',_shutdown=_shutdown)


def open_migration_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='migrate')


def open_bootstrap_resources(config, *, custody, deadline):
    return _open(config,custody=custody,deadline=deadline,mode='bootstrap')
