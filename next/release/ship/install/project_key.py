"""J1 project unlock key (day 3, row feb9dc8b): INTERFACE ONLY, intentional RED. No function here has an effect yet; each refuses `install_not_wired:<name>`.

WHAT IT IS. The installer-side private opener for ONE option-C project root: 32 raw bytes in a host-private key file, outside the vault, one file per project. It admits the file and
returns a FRESH read-only descriptor that Core's existing `runtime.custody_fs.read_unlock(fd)` already accepts (regular file, effective-uid owner, no group/other bits, one link, 32 bytes).
It never returns, stores, logs or puts key bytes in an exception. It selects no daemon, env var, argv, keychain, cloud store or second custody mechanism.

THE CLOSED REFERENCE (nonsecret; Core's decoder, Finn's file, must carry exactly these keys under `custody_unlock` and compare project_id with the config's own project_id):
  {"kind": "project-key-file", "path": <canonical absolute>, "key_id": <[A-Za-z0-9_-]{1,64}>, "identity": <execution-identity name>, "project_id": <uuid of the config's project>}
  path      canonical, absolute, no `..`, final component == key_id + ".key" (the file name carries the id).
  key_id    DERIVED: "pk1-" + sha256(b"cortex-project-key-id-v1\\0" + key).hexdigest()[:32]. A swapped, wrong or installation key is refused by the opener before Core is touched. Core keeps it opaque.
  identity  the OS execution identity (account name) that must equal the CURRENT process's effective account. Root (euid 0) is refused.

SEAM (what Cox, Finn and Bob call):
  derive_key_id(key: bytes) -> str
  validate_reference(ref, *, project_id) -> dict            closed shape, no IO
  open_unlock(ref, *, project_id, installation_key_id) -> int   admit + return ONE fresh non-inheritable read-only fd (the caller closes it)
  UnlockSession(ref, *, project_id, installation_key_id, deadline)   one lead session: .fresh_fd() re-admits every call and returns a NEW fd; .close(); refuses after close, after the
                                                                    original deadline, in another process, when copied or pickled.

ADMISSION (every check before any descriptor leaves this module; no repair, no chmod, no create):
  shape -> identity (account == reference, not root) -> path chain opened from "/" with O_NOFOLLOW|O_DIRECTORY, parent directory owned by the effective uid, mode 0700, ancestors owned by root or
  the effective uid and not group/other-writable -> leaf opened O_NOFOLLOW: regular file, effective-uid owner, mode exactly 0400, one link, size 32, no ACL, filesystem that honours ownership ->
  read once to derive the id: must equal reference.key_id and must NOT equal installation_key_id -> the name still resolves to the same device/inode -> lseek(0) and return.
REFUSALS (`KeyRefusal.reason`, never a path or a byte): unlock_reference_invalid, unlock_reference_foreign, unlock_identity_mismatch, unlock_identity_privileged, unlock_path_unsafe, unlock_key_absent,
unlock_key_unsafe, unlock_acl_present, unlock_mount_unsafe, unlock_key_mismatch, unlock_key_installation, unlock_io_failed, unlock_session_closed, unlock_session_expired, unlock_session_foreign_process,
unlock_session_copy, unlock_deadline_invalid; this skeleton alone returns install_not_wired:<name>.
NOT CLAIMED: native isolation, a second OS user, reboot, Linux, any admitted host identity. Fixtures are not native credit."""
import re

KIND = 'project-key-file'
REFERENCE_KEYS = frozenset({'kind', 'path', 'key_id', 'identity', 'project_id'})
KEY_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
KEY_ID_DOMAIN = b'cortex-project-key-id-v1\x00'
KEY_ID_PREFIX = 'pk1-'
KEY_BYTES = 32


class KeyRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _not_wired(name):
    raise KeyRefusal('install_not_wired:' + name)


def derive_key_id(key):
    _not_wired('derive_key_id')


def validate_reference(ref, *, project_id):
    _not_wired('validate_reference')


def open_unlock(ref, *, project_id, installation_key_id):
    _not_wired('open_unlock')


class UnlockSession:
    def __init__(self, ref, *, project_id, installation_key_id, deadline):
        _not_wired('UnlockSession')

    def fresh_fd(self):
        _not_wired('UnlockSession.fresh_fd')

    def close(self):
        _not_wired('UnlockSession.close')
