"""ship/install roles (J02): create the contract's login roles before the unchanged migrations, set the exact memberships after
them, close the finite logins only after the one seed's durable acknowledgement. Pure functions that render psql scripts from the
ONE roles block of runtime-operations-v1.json; psql (already in the Core image) is the only executor."""
import json
import re
from pathlib import Path

NEXT = Path(__file__).resolve().parents[3]
CONTRACT = NEXT / 'contracts/runtime/runtime-operations-v1.json'
# Physical names. The two groups and the installer login are fixed by the unchanged migrations (002, 004); the rest are ours.
PHYSICAL = {
    'bootstrap': 'kaidera-runtime-bootstrap', 'migrator': 'kaidera-runtime-migrator',
    'request_group': 'kaidera-runtime-core-request', 'verifier_owner': 'kaidera-runtime-core-verifier',
    'api_request': 'kaidera-runtime-api-request', 'probe': 'kaidera-runtime-probe',
    'supervisor': 'kaidera-runtime-supervisor', 'observer': 'kaidera-runtime-observer',
    'installer_enrollment': 'kaidera-runtime-core-installer',
}
KEYS = ('login', 'superuser', 'bypassrls', 'inherit', 'lifetime', 'createdb', 'createrole', 'replication', 'memberships')
MIN_SECRET = 20
_SAFE = re.compile(r'^[A-Za-z0-9._/-]+$')


class RoleRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_roles(path=None):
    try:
        roles = json.loads(Path(path or CONTRACT).read_text())['roles']
        ok = set(roles) == set(PHYSICAL) and all(all(k in roles[n] for k in KEYS) for n in roles)
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise RoleRefusal('roles_contract_invalid')
    return roles


def physical_name(logical):
    try:
        return PHYSICAL[logical]
    except KeyError:
        raise RoleRefusal('roles_contract_invalid') from None


def _q(logical):
    return '"' + physical_name(logical) + '"'


def _flags(spec):
    b = lambda on, word: word if on else 'NO' + word
    return ' '.join([b(spec['login'], 'LOGIN'), b(spec['superuser'], 'SUPERUSER'), b(spec['createdb'], 'CREATEDB'),
                     b(spec['createrole'], 'CREATEROLE'), b(spec['inherit'], 'INHERIT'), b(spec['replication'], 'REPLICATION'),
                     b(spec['bypassrls'], 'BYPASSRLS')])


def _creatable(roles):
    """Logins this phase creates: everything that logs in except bootstrap (core-init made it). Finite ones first."""
    order = ('migrator', 'installer_enrollment', 'api_request', 'probe', 'supervisor', 'observer')
    return [n for n in order if roles[n]['login']]


def render_pre(roles, secret_dir):
    if not _SAFE.match(str(secret_dir)):
        raise RoleRefusal('secret_dir_unsafe')
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;', "SET LOCAL log_statement = 'none';", "SET LOCAL log_min_error_statement = 'panic';",
           "CREATE FUNCTION pg_temp.need(ok boolean, why text) RETURNS void LANGUAGE plpgsql AS $f$ BEGIN IF NOT ok THEN "
           "RAISE EXCEPTION '%', why USING ERRCODE = '22023'; END IF; END $f$;"]
    for n in _creatable(roles):
        name = physical_name(n)
        out += [f"\\set pw `cat '{secret_dir}/{name}'`",
                f"SELECT pg_temp.need(length(:'pw') >= {MIN_SECRET}, 'secret_missing_or_short:{name}');",
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN CREATE ROLE {_q(n)}; END IF; END $$;",
                f"ALTER ROLE {_q(n)} {_flags(roles[n])} PASSWORD :'pw';", '\\unset pw']
    return '\n'.join(out + ['COMMIT;', ''])


def render_post(roles):
    ours = [physical_name(n) for n in roles]
    wanted = [(physical_name(n), g) for n in roles for g in roles[n]['memberships']]
    if any(g not in PHYSICAL.values() for _, g in wanted):  # the contract names groups by their physical name
        raise RoleRefusal('roles_contract_invalid')
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;']
    for member, group in wanted:
        out.append(f'GRANT "{group}" TO "{member}" WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;')
    names = ', '.join(f"'{x}'" for x in ours)
    pairs = ', '.join(f"('{m}', '{g}')" for m, g in wanted) or "('', '')"
    out += ['DO $$ DECLARE bad text; BEGIN',
            "  SELECT string_agg(m.rolname || ' > ' || g.rolname, ', ') INTO bad FROM pg_auth_members a",
            '    JOIN pg_roles m ON m.oid = a.member JOIN pg_roles g ON g.oid = a.roleid',
            f'    WHERE (m.rolname IN ({names}) OR g.rolname IN ({names})) AND (m.rolname, g.rolname) NOT IN (VALUES {pairs});',
            "  IF bad IS NOT NULL THEN RAISE EXCEPTION 'unexpected role membership: %', bad USING ERRCODE = '42501'; END IF;",
            'END $$;', 'COMMIT;', '']
    return '\n'.join(out)


def render_close(roles, seed_ack):
    if seed_ack is not True:
        raise RoleRefusal('seed_not_acknowledged')
    finite = [n for n in ('installer_enrollment', 'migrator', 'bootstrap') if roles[n]['lifetime'] != 'service']
    out = ['\\set ON_ERROR_STOP on', 'BEGIN;']
    for n in finite:
        name = physical_name(n)
        out += [f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN ALTER ROLE {_q(n)} NOLOGIN; END IF; END $$;",
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{name}' AND pid <> pg_backend_pid();"]
    out += ['COMMIT;', '']
    return '\n'.join(out)
