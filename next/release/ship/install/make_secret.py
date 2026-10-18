"""S1 make_secret: INTERFACE ONLY (day 3, intentional RED). There is no implementation here and no effect: the body refuses `install_not_wired:make_secret`.

Frozen inputs (not chosen here): Core's `open_installer_custody(config, *, unlock_fd, key_id, deadline)` and `owner.import_credential(purpose, bytes)` from the accepted custody contract
(docs/handoffs/2026-10-18_cox_hale_s0-amend-contract.md, sha256 35977ae5…8ac85). That Core API is NOT at this base (Finn's Core1 source is still building it), so the owner type is resolved
from Core at call time; at this base it is unbound and the call refuses. The J06 human choice (where the unlock key and the credentials come from) is NOT made here: the producer is an
explicit argument and there is no default, so an undefined J06 is a typed refusal.

  make_secret(owner, *, producer, bootstrap_sink, deadline) -> {"imported": [five purposes in the frozen order], "bootstrap_delivered": True}

owner           the actual Core InstallerCustody, already opened by the caller with `with open_installer_custody(...) as owner:`; exact type, never a dict, factory, resolver or subclass.
producer        exposes `bytes_for(purpose) -> bytes`, called exactly once per purpose in the frozen order. It is the J06 seam: no default exists in this module, no random source,
                no environment, no argv, no file. A missing producer refuses `j06_producer_undefined`.
bootstrap_sink  one-shot callable(bytes) that carries the SAME bootstrap bytes that were imported to the S4 prelude (stock initdb password transfer). Called once, only after all five imports
                succeeded, so no database is initialised with a credential custody does not hold.
deadline        the caller's ONE original absolute monotonic deadline (finite, not bool); never renewed.
Order and lifetime: validate owner, producer, deadline -> produce all five -> validate all five (type bytes, length >= roles.MIN_SECRET, pairwise distinct) -> import in the frozen order
(bootstrap, migrator, api_request, probe, installer_enrollment) -> deliver bootstrap once. Nothing is stored after the call, nothing is written, printed, logged or put in an exception.
Refusals (typed, `MakeSecretRefusal.reason`): custody_owner_unbound, custody_owner_type, j06_producer_undefined, deadline_invalid, deadline_expired, secret_invalid:<purpose>,
secret_reused, import_failed:<purpose>, bootstrap_sink_failed; this skeleton alone returns install_not_wired:make_secret."""
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
    """The actual Core InstallerCustody class, or None while Core does not provide it (this base). Component tests replace this function with a NAMED DOUBLE; nothing in
    production passes a type."""
    return None


def make_secret(owner, *, producer, bootstrap_sink, deadline):
    raise MakeSecretRefusal('install_not_wired:make_secret')
