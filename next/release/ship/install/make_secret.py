"""S1 make_secret: INTERFACE ONLY (day 3, corrected for HALE-S1-LIFETIME-002; intentional RED). There is no implementation here and no effect: the body refuses `install_not_wired:make_secret`.

  make_secret(owner, *, producer, deadline) -> {"imported": [five purposes in the frozen order], "bootstrap_delivered": True}

THE ONE CALLER AND THE ORDER (cox order A; Nix's finite installer orchestrator, a later row):
  write_config (S2) -> render.finite_bootstrap(config_path, plan=, unlock_fd=, key_id=, operation_key=, record=, engine=, deadline=D)   [S4; yields the ACTUAL Core InstallerCustody at IMPORT]
  -> make_secret(owner, producer=<J06 producer>, deadline=D)   [this module, the SAME D, inside S4's `with`]
  -> back in S4: its own admission, owner.run(operation_key) once (Nix provision_sequence), reverse cleanup, custody exit, and only then the completion object.
No caller-supplied sink or callback exists. S1 hands the SAME imported bootstrap bytes to the FIXED S4 receiver `render.receive_bootstrap(owner, bootstrap)`, resolved by `_receiver()`. The producer
is the one remaining external object: the J06 seam (undefined, no default, an explicit argument). Reference only, neither is at this base: S4 head dd9c2f80a71e780b1fc361ff31a7b3264b4c82e8
(render.py blob 7e94cc47da7ae3c1cd780d96b2fa9e200e14037f) and its selected Core 90daf7ea (custody.py blob 3c9713101fb24451896eedadaabcf7c49ad85f23). Source delivery is PARTIAL and unreviewed.

owner      the actual Core InstallerCustody that `finite_bootstrap` yielded, exact type only (never a dict, factory, resolver or subclass), resolved by `_owner_type()`.
producer   exposes `bytes_for(purpose) -> bytes`, called exactly once per purpose in the frozen order. No default, no random source, no environment, no argv, no file. Missing: `j06_producer_undefined`.
deadline   the ONE original absolute monotonic deadline. It must be a finite non-bool number and equal (type and value) to the owner's own original deadline `owner._deadline`; a caller can never
           substitute or extend it. It is never renewed.

PRIVATE ADMISSION (the join that was missing). S1 reads the owner's private slots (trusted-code composition, not process isolation) through Core's existing functions, resolved by `_core()`:
  core._current_installer(owner._config, owner._vault, owner._deadline) must return exactly `owner`, and the slots must read: owner._stage == 'IMPORT', not owner._busy, not owner._tainted,
  not owner._closed, owner._deadline the same object S1 captured at entry and equal to the `deadline` argument, now < owner._deadline, and owner._imported == the purposes S1 itself has imported so
  far (empty before the first import, then one more after each). A class comparison, a caller flag or the result of the call is never admission. A failure is `custody_context`, `deadline_expired` or
  `deadline_not_original`.
  WHEN: before the producer is asked; after EVERY producer.bytes_for returns; before every import; after the fifth import and BEFORE the receiver; after the receiver returns. Success is reported only
  after that last admission holds with all five purposes imported. S1 never calls owner.run, never writes an owner slot, never sets a stage, flag or default.
FAILURE UNWIND. From the first read of the owner's slots onward every refusal, and any BaseException from a callback, first calls core._installer_fail(owner) (the existing path) and then raises.
  A refusal that the caller catches inside the still-open block therefore leaves the owner tainted: S4's own admission and owner.run refuse, and a second make_secret is refused `custody_context`
  with the producer never asked again. Refusals decided before the owner is read leave it untouched (custody_owner_unbound, custody_owner_type, bootstrap_receiver_unbound,
  j06_producer_undefined, deadline_invalid, and an expired argument).
REENTRY. A call-scoped ContextVar holding the active owner (the device S4 uses for its `_BOOTSTRAP`) lives for the duration of the call only. A second make_secret entered from a producer, import or
  receiver callback is refused `make_secret_reentered` and invalidates the active owner. Nothing survives the call.

ORDER: resolve owner type, Core and receiver -> check owner type -> producer defined -> deadline argument (invalid, expired) -> reentry -> admission and original deadline -> produce all five (admit after
each) -> validate all five (type bytes, length >= roles.MIN_SECRET, pairwise distinct) -> admit before each import, import in the frozen order (bootstrap, migrator, api_request, probe,
installer_enrollment) -> admit with all five held -> receiver(owner, the bootstrap bytes), once -> admit -> return names only. Nothing is stored after the call, written, printed, logged or put in an
exception; an exception raised by a callback is replaced by the typed refusal and never chained.
REFUSALS (`MakeSecretRefusal.reason`): custody_owner_unbound, custody_owner_type, bootstrap_receiver_unbound, j06_producer_undefined, deadline_invalid, deadline_expired, deadline_not_original,
custody_context, make_secret_reentered, producer_failed:<purpose>, secret_invalid:<purpose>, secret_reused, import_failed:<purpose>, bootstrap_receiver_failed; this skeleton alone returns
install_not_wired:make_secret. `bootstrap_sink_failed` of the first interface is retired with the sink."""
try:
    from . import roles as R  # packaged layout: cortex_ship.install
except ImportError:
    import roles as R

PURPOSES = R.A_PURPOSES          # the frozen import order
MIN_SECRET = R.MIN_SECRET


class MakeSecretRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _owner_type():
    """The actual Core InstallerCustody class (cortex_core.runtime.custody), or None while Core does not provide it (this base). Component tests replace this function with a NAMED DOUBLE; nothing
    in production passes a type."""
    return None


def _core():
    """Core's private admission surface: an object with `_current_installer(config, custody, deadline)` and `_installer_fail(owner)` (cortex_core.runtime.custody), or None while it is absent
    (this base). Component tests replace this function with a NAMED DOUBLE; no caller passes it."""
    return None


def _receiver():
    """The fixed S4 receiver `receive_bootstrap(owner, bootstrap)` (cortex_ship.run.render), or None while it is absent (this base). Component tests replace this function with a NAMED DOUBLE; no
    caller passes a callable."""
    return None


def make_secret(owner, *, producer, deadline):
    raise MakeSecretRefusal('install_not_wired:make_secret')
