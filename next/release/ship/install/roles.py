"""ship/install roles (J02, rework): create the approved login roles before the unchanged migrations, set the exact membership after them, close the
finite logins only after the one seed's durable acknowledgement. Pure functions that render psql scripts.
Policy: the five option-A purposes projected from the ONE roles block of runtime-operations-v1.json (flags unchanged; supervisor and observer are not A,
J07). Names and secret references: from the published Core config carrier (cortex_core.runtime.bootstrap_config: roles[purpose].login/.secret_ref,
vault_root); a reference resolves to vault_root/secret_ref. No second config parser here."""
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path

NEXT = Path(__file__).resolve().parents[3]
CONTRACT = NEXT / 'contracts/runtime/runtime-operations-v1.json'
A_PURPOSES = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
# Fixed by the unchanged migrations 002 (the two NOLOGIN groups) and 004 (the installer login); config may not rename them.
REQUEST_GROUP = 'kaidera-runtime-core-request'
VERIFIER_GROUP = 'kaidera-runtime-core-verifier'
GROUPS = (REQUEST_GROUP, VERIFIER_GROUP)
INSTALLER_LOGIN = 'kaidera-runtime-core-installer'
KEYS = ('login', 'superuser', 'bypassrls', 'inherit', 'lifetime', 'createdb', 'createrole', 'replication', 'memberships')
MIN_SECRET = 20
_BAD_PATH = set('\'"`\\\n\r\x00$')


class RoleRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Binding:
    policy: dict
    logins: dict
    secrets: dict
    vault_root: str


def load_roles(path=None):
    try:
        roles = json.loads(Path(path or CONTRACT).read_text())['roles']
        ok = all(all(k in roles[p] for k in KEYS) for p in A_PURPOSES)
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise RoleRefusal('roles_contract_invalid')
    return {p: roles[p] for p in A_PURPOSES}


def _shared(cfg):
    """Everything a mode view carries besides its mode and its own purposes: the identity of the ONE decoded snapshot."""
    return {k: v for k, v in vars(cfg).items() if k not in ('mode', 'roles')}


def bind(configs, roles=None):
    """Join the seed/serve/migrate views of ONE Core snapshot. Refuses views of different snapshots (HALE-A1-NIX-JOIN-001: every shared field must
    be equal) and a combined set whose logins or resolved secret paths are not unique, because each view only checks its own purposes."""
    policy = roles or load_roles()
    roots, merged, shared = set(), {}, []
    for cfg in configs:
        roots.add(str(cfg.vault_root))
        shared.append(_shared(cfg))
        for purpose, role in dict(cfg.roles).items():
            if purpose not in A_PURPOSES:
                raise RoleRefusal(f'role_config_unknown:{purpose}')
            pair = (str(role.login), str(role.secret_ref))
            if merged.setdefault(purpose, pair) != pair:
                raise RoleRefusal(f'role_config_conflict:{purpose}')
    if len(roots) != 1:
        raise RoleRefusal('role_config_conflict:vault_root')
    missing = [p for p in A_PURPOSES if p not in merged]
    if missing:
        raise RoleRefusal('role_config_incomplete:' + ','.join(missing))
    root = roots.pop()
    logins, secrets = {}, {}
    for purpose in A_PURPOSES:
        login, ref = merged[purpose]
        if login in GROUPS or (purpose == 'installer_enrollment') != (login == INSTALLER_LOGIN):
            raise RoleRefusal(f'role_name_conflict:{purpose}')
        if not login or _BAD_PATH & set(login):
            raise RoleRefusal(f'role_name_unsafe:{purpose}')
        parts = PurePosixPath(ref).parts
        if (ref.startswith('/') or '..' in parts or '.' in parts or not parts or _BAD_PATH & set(ref + root)
                or not root.startswith('/') or '..' in PurePosixPath(root).parts):
            raise RoleRefusal(f'role_ref_unsafe:{purpose}')
        logins[purpose], secrets[purpose] = login, f'{root.rstrip("/")}/{ref}'
    if len(set(logins.values())) != len(logins):
        raise RoleRefusal('role_config_collision:login')
    paths = [str(PurePosixPath(v)) for v in secrets.values()]
    if len(set(paths)) != len(paths):
        raise RoleRefusal('role_config_collision:secret')
    # Snapshot identity LAST: the typed collision reasons above stay decisive for mixed snapshots (the Core carrier's source_sha256 differs between them).
    if any(other != shared[0] for other in shared[1:]):
        raise RoleRefusal('role_config_conflict:snapshot')
    return Binding(policy, logins, secrets, root)


def _q(name):
    return '"' + name + '"'


def _flags(spec):
    b = lambda on, word: word if on else 'NO' + word
    return ' '.join([b(spec['login'], 'LOGIN'), b(spec['superuser'], 'SUPERUSER'), b(spec['createdb'], 'CREATEDB'),
                     b(spec['createrole'], 'CREATEROLE'), b(spec['inherit'], 'INHERIT'), b(spec['replication'], 'REPLICATION'),
                     b(spec['bypassrls'], 'BYPASSRLS')])


def render_pre(b):
    """Logins this phase creates: everything that logs in except bootstrap (core-init made it). Finite ones first."""
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;', "SET LOCAL log_statement = 'none';", "SET LOCAL log_min_error_statement = 'panic';",
           "CREATE FUNCTION pg_temp.need(ok boolean, why text) RETURNS void LANGUAGE plpgsql AS $f$ BEGIN IF NOT ok THEN "
           "RAISE EXCEPTION '%', why USING ERRCODE = '22023'; END IF; END $f$;"]
    for p in ('migrator', 'installer_enrollment', 'api_request', 'probe'):
        login = b.logins[p]
        out += [f"\\set pw `cat '{b.secrets[p]}'`",
                f"SELECT pg_temp.need(length(:'pw') >= {MIN_SECRET}, 'secret_missing_or_short:{login}');",
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') THEN CREATE ROLE {_q(login)}; END IF; END $$;",
                f"ALTER ROLE {_q(login)} {_flags(b.policy[p])} PASSWORD :'pw';", '\\unset pw']
    return '\n'.join(out + ['COMMIT;', ''])


def render_post(b):
    wanted = [(b.logins[p], g) for p in A_PURPOSES for g in b.policy[p]['memberships']]
    if any(g not in GROUPS for _, g in wanted):
        raise RoleRefusal('roles_contract_invalid')
    ours = list(b.logins.values()) + list(GROUPS)
    names = ', '.join(f"'{x}'" for x in ours)
    pairs = ', '.join(f"('{m}', '{g}')" for m, g in wanted) or "('', '')"
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;']
    out += [f'GRANT "{g}" TO "{m}" WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;' for m, g in wanted]
    out += ['DO $$ DECLARE bad text; BEGIN',
            "  SELECT string_agg(m.rolname || ' > ' || g.rolname, ', ') INTO bad FROM pg_auth_members a",
            '    JOIN pg_roles m ON m.oid = a.member JOIN pg_roles g ON g.oid = a.roleid',
            f'    WHERE (m.rolname IN ({names}) OR g.rolname IN ({names})) AND (m.rolname, g.rolname) NOT IN (VALUES {pairs});',
            "  IF bad IS NOT NULL THEN RAISE EXCEPTION 'unexpected role membership: %', bad USING ERRCODE = '42501'; END IF;",
            'END $$;', 'COMMIT;', '']
    return '\n'.join(out)


def render_close(b, seed_ack):
    if seed_ack is not True:
        raise RoleRefusal('seed_not_acknowledged')
    finite = [p for p in ('installer_enrollment', 'migrator', 'bootstrap') if b.policy[p]['lifetime'] != 'service']
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;']
    for p in finite:
        login = b.logins[p]
        out += [f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') THEN ALTER ROLE {_q(login)} NOLOGIN; END IF; END $$;",
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{login}' AND pid <> pg_backend_pid();"]
    out += ['COMMIT;', '']
    return '\n'.join(out)
