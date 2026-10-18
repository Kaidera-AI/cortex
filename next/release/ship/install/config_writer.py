"""S2 write_config (day 3): the ONE installer writer of the ONE Core config file `<vault>/<project>/config/cortex.json` (Core's closed carrier, cox format, nix writes).

No parser, path rule or store of our own: the candidate document is admitted by Core's own decoder in every mode view (`serve`, `migrate`, `seed`; the Core `install` mode is not
at this base) and by the sole role-name validator (`roles.bind_logins`) BEFORE any filesystem effect; it is written private (0600), atomic and no-follow with no clobber, and re-read
through Core's own private-file reader. The file holds references (`secrets/cortex/...`) and non-secret identity only; no secret, password, token or key exists here.
  python3 -I -B config_writer.py [--core-src DIR] --vault DIR --project KEY --port N [--owner-uid U]   (exit 0 result on stdout; exit 2 one typed refusal on stderr)
  write_config(vault, project, port, *, new_id=None, owner_uid=None) -> {'result': 'written'|'unchanged', 'path', 'vault_root', 'source_sha256', 'installation_id'}
Typed refusals (Core's own reasons pass through unchanged): config_input, config_identifier, config_vault_missing, config_path_unsafe, config_owner, config_permissions,
config_exists_different, config_write_failed, config_publish_incomplete, config_readback; Core: private_file_required, config_transport_unratified, config_shape, ..."""
import importlib.util
import json
import os
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath

if __name__ == '__main__' and '--core-src' in sys.argv[:-1]:   # explicit Core source (install.sh passes it); python3 -I ignores PYTHONPATH
    sys.path.insert(0, sys.argv[sys.argv.index('--core-src') + 1])
elif importlib.util.find_spec('cortex_core') is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))   # source layout: next/release/ship/install -> next/src
from cortex_core.runtime.bootstrap_config import MODE_PURPOSES, NAME, decode_config, read_config_file  # noqa: E402
from cortex_core.runtime.signed_member import Refusal  # noqa: E402

try:
    from . import roles as R  # packaged layout: cortex_ship.install
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # run as a script under -I: the script directory is not on sys.path
    import roles as R

VIEWS = ('serve', 'migrate', 'seed')
FILE = 'cortex.json'
PURPOSES = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
# Non-secret option A identity. Logins follow the sole validator (the installer login is fixed by migration 004); references are metadata, never values.
LOGINS = {'bootstrap': 'cortex_bootstrap', 'migrator': 'cortex_migrator', 'api_request': 'cortex_api_request', 'probe': 'cortex_probe',
          'installer_enrollment': R.INSTALLER_LOGIN}
REFS = {'bootstrap': 'secrets/cortex/pg_boot', 'migrator': 'secrets/cortex/pg_mig', 'api_request': 'secrets/cortex/pg_req',
        'probe': 'secrets/cortex/pg_probe', 'installer_enrollment': 'secrets/cortex/pg_enrol'}
CLIENT_REF = 'secrets/cortex/client'
CA_REF = 'secrets/cortex/db_ca'
BIND = {'host': '0.0.0.0', 'port': 8000}            # the API listener inside its pod (operations contract: health.api_internal_port)
DATABASE = {'host': '127.0.0.1', 'port': 5432, 'name': 'cortex'}   # in-pod loopback (operations contract: core_port; never published)
LIMITS = {'request_seconds': 10, 'shutdown_seconds': 15}           # request_ms 10000, shutdown_ms 15000 of the operations contract
_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY


def _input(ok, reason='config_input'):
    if not ok:
        raise Refusal(reason)


def _walk(path):
    """No-follow descent from / to an existing directory; returns an open fd. Any symlink component is config_path_unsafe, a missing one config_vault_missing."""
    _input(type(path) is str and path.startswith('/') and not path.startswith('//') and str(PurePosixPath(path)) == path and path != '/'
           and not {'.', '..'} & set(PurePosixPath(path).parts))
    fd = os.open('/', _FLAGS)
    try:
        for part in PurePosixPath(path).parts[1:]:
            try:
                nxt = os.open(part, _FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise Refusal('config_vault_missing') from None
            except OSError:
                raise Refusal('config_path_unsafe') from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_dir(fd, owner, *, strict):
    try:
        st = os.fstat(fd)
    except OSError:
        raise Refusal('config_write_failed') from None   # a failing check is a typed refusal, never a raw OSError
    if st.st_uid != owner:
        raise Refusal('config_owner')
    if st.st_mode & (0o077 if strict else 0o022):
        raise Refusal('config_permissions')


def _child(parent_fd, name, owner):
    """Open an existing private child directory without following, or None when it does not exist yet."""
    try:
        fd = os.open(name, _FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refusal('config_path_unsafe') from None
    try:
        _check_dir(fd, owner, strict=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _make_child(parent_fd, name, owner):
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, _FLAGS, dir_fd=parent_fd)
    except OSError:
        raise Refusal('config_write_failed') from None
    try:   # the newly acquired descriptor is owned here: every failure closes it and is typed
        os.fchmod(fd, 0o700)
        _check_dir(fd, owner, strict=True)
    except OSError:
        os.close(fd)
        raise Refusal('config_write_failed') from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _document(project, port, ids, vault_root):
    installation, tenant, project_id = ids
    return {'version': 1, 'installation_id': installation, 'tenant_id': tenant, 'project_id': project_id,
            'project_names': {project: project_id}, 'ai_mode': 'off', 'vault_root': vault_root,
            'bind': dict(BIND), 'publish': {'host': '127.0.0.1', 'port': port}, 'client': {'credential_ref': CLIENT_REF},
            'database': dict(DATABASE, tls={'mode': 'verify-full', 'root_cert_ref': CA_REF}),
            'roles': {p: {'login': LOGINS[p], 'secret_ref': REFS[p]} for p in PURPOSES}, 'limits': dict(LIMITS)}


def _admit(doc):
    """Core's own decoder in every mode view over the SAME bytes, then the sole role-name validator. -> (raw bytes, source_sha256)."""
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
    views = {mode: decode_config(raw, mode=mode) for mode in VIEWS}
    digests = {view.source_sha256 for view in views.values()}
    _input(len(digests) == 1, 'config_readback')
    for mode, view in views.items():
        _input(sorted(view.roles) == sorted(MODE_PURPOSES[mode]), 'config_readback')
    R.bind_logins(dict(views['seed'].role_logins), views['seed'].vault_root)
    return raw, digests.pop()


def _read_back(path):
    views = {mode: read_config_file(path, mode=mode) for mode in VIEWS}   # Core's no-follow reader: private, owned, one link
    digests = {view.source_sha256 for view in views.values()}
    _input(len(digests) == 1, 'config_readback')
    return views['seed'], digests.pop()


def write_config(vault, project, port, *, new_id=None, owner_uid=None):
    _input(type(project) is str, 'config_identifier')
    _input(NAME.fullmatch(project) is not None, 'config_identifier')
    _input(type(port) is int and 1 <= port <= 65535)
    _input(owner_uid is None or (type(owner_uid) is int and owner_uid >= 0))
    owner = os.geteuid() if owner_uid is None else owner_uid
    make_id = new_id or (lambda: str(uuid.uuid4()))
    root_path = f'{vault}/{project}'
    vault_fd = _walk(vault)
    fds = [vault_fd]
    try:
        _check_dir(vault_fd, owner, strict=False)
        root_fd = _child(vault_fd, project, owner)
        cfg_fd = None
        if root_fd is not None:
            fds.append(root_fd)
            cfg_fd = _child(root_fd, 'config', owner)
            if cfg_fd is not None:
                fds.append(cfg_fd)
        path = f'{root_path}/config/{FILE}'
        if cfg_fd is not None:
            try:
                leaf = os.stat(FILE, dir_fd=cfg_fd, follow_symlinks=False)
            except FileNotFoundError:
                leaf = None
            except OSError:
                raise Refusal('config_path_unsafe') from None
            if leaf is not None:
                _input(stat.S_ISREG(leaf.st_mode), 'config_path_unsafe')
                existing, digest = _read_back(path)   # Core's reasons pass through (private_file_required, config_transport_unratified, ...)
                again = _document(project, port, (existing.installation_id, existing.tenant_id, existing.project_id), root_path)
                _input(_admit(again)[1] == digest, 'config_exists_different')
                return _result('unchanged', path, root_path, digest, existing.installation_id)
        ids = (make_id(), make_id(), make_id())
        raw, digest = _admit(_document(project, port, ids, root_path))   # every refusal of the candidate happens before the first directory is made
        if root_fd is None:
            root_fd = _make_child(vault_fd, project, owner)
            fds.append(root_fd)
        if cfg_fd is None:
            cfg_fd = _make_child(root_fd, 'config', owner)
            fds.append(cfg_fd)
        _publish(cfg_fd, raw)
        written, got = _read_back(path)
        _input(got == digest, 'config_readback')
        return _result('written', path, root_path, digest, written.installation_id)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _rollback(cfg_fd, tmp, state, ident):
    """Undo ONLY what this writer made: its own linked final (identity checked, never a file another actor put there) and its own temp. Returns the typed reason. A final that could not
    be removed is `config_publish_incomplete` (exact remaining state: final plus temp, two links, which Core keeps refusing; recovery = a person removes the temp name). A temp whose removal
    keeps failing after the final is gone stays as one private, ignored, one-link leaf (disclosed, no recovery mechanism here)."""
    incomplete = False
    if state['linked']:
        try:
            leaf = os.stat(FILE, dir_fd=cfg_fd, follow_symlinks=False)
            if ident is not None and (leaf.st_dev, leaf.st_ino) == ident:
                try:
                    os.unlink(FILE, dir_fd=cfg_fd)
                except OSError:
                    incomplete = True
        except FileNotFoundError:
            pass
        except OSError:
            incomplete = True
    if state['tmp']:
        try:
            os.unlink(tmp, dir_fd=cfg_fd)
        except OSError:
            pass
    try:
        os.fsync(cfg_fd)   # best effort: the removal's durability is unproven if this fails, and the failure is still what is reported
    except OSError:
        pass
    return 'config_publish_incomplete' if incomplete else 'config_write_failed'


def _publish(cfg_fd, raw):
    """All-or-none: private temp, fsync, link (never replace), directory fsync, remove the temp, directory fsync AGAIN. Success is reported only after the second fsync. Any failure after the
    link rolls back this writer's own final and temp (see _rollback), so a reported failure never leaves a config Core accepts and a two-link unreadable final is never silently kept."""
    tmp = f'.{FILE}.{uuid.uuid4().hex}'
    state = {'tmp': False, 'linked': False}
    ident = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=cfg_fd)
        state['tmp'] = True
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            st = os.fstat(fd)
            ident = (st.st_dev, st.st_ino)
        finally:
            os.close(fd)
        try:
            os.link(tmp, FILE, src_dir_fd=cfg_fd, dst_dir_fd=cfg_fd, follow_symlinks=False)
        except FileExistsError:
            _rollback(cfg_fd, tmp, state, ident)   # nothing of ours was linked: only the temp is removed
            raise Refusal('config_exists_different') from None
        state['linked'] = True
        os.fsync(cfg_fd)
        os.unlink(tmp, dir_fd=cfg_fd)
        state['tmp'] = False
        os.fsync(cfg_fd)
    except OSError:
        raise Refusal(_rollback(cfg_fd, tmp, state, ident)) from None


def _result(state, path, root, digest, installation):
    return {'result': state, 'path': path, 'vault_root': root, 'source_sha256': digest, 'installation_id': installation}


def main(argv):
    values = {}
    try:
        pending = list(argv)
        while pending:
            name = pending.pop(0)
            _input(name in ('--core-src', '--vault', '--project', '--port', '--owner-uid') and name not in values and pending)
            values[name] = pending.pop(0)
        _input({'--vault', '--project', '--port'} <= set(values))
        for key in ('--port', '--owner-uid'):
            if key in values:
                _input(values[key].isascii() and values[key].isdigit())
        result = write_config(values['--vault'], values['--project'], int(values['--port']),
                              owner_uid=int(values['--owner-uid']) if '--owner-uid' in values else None)
    except Refusal as refusal:
        print(json.dumps({'reason': refusal.reason, 'unit': 'write_config'}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
