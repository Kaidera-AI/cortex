"""ship/install roles (J02, rework; J06 sole catalog validator `validate_catalog`): create the approved login roles before the unchanged migrations, set the exact membership after them, close the
finite logins only after the one seed's durable acknowledgement. Pure functions that render psql scripts.
Policy: the five option-A purposes projected from the ONE roles block of runtime-operations-v1.json (flags unchanged; supervisor and observer are not A,
J07). Names and secret references: from the published Core config carrier (cortex_core.runtime.bootstrap_config: roles[purpose].login/.secret_ref,
vault_root); a reference resolves to vault_root/secret_ref. No second config parser here."""
import copy
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path

_PACKED = Path(__file__).resolve().with_name('runtime-operations-v1.json')
if __name__.startswith('cortex_ship.'):  # packaged into the Core closure: the contract ships next to the module, never a tree path
    CONTRACT = _PACKED
else:
    NEXT = Path(__file__).resolve().parents[3]
    CONTRACT = _PACKED if _PACKED.is_file() else NEXT / 'contracts/runtime/runtime-operations-v1.json'
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


# ---- J06: the SOLE role-catalog policy validator. Cox's read-only observer (runtime/catalog.py) returns the raw observation; this admits or refuses it. ----
INTENDED_REVISION = {'bootstrap': 0, 'migrator': 0, 'api_request': 1, 'probe': 1, 'installer_enrollment': 1}  # intent, never an observed authority (Cox v2 proposal)
BOUND = ('api_request', 'probe', 'installer_enrollment')
_FLAG_KEYS = ('login', 'superuser', 'createdb', 'createrole', 'inherit', 'replication', 'bypassrls')
_EDGE_KEYS = ('member', 'oid', 'set', 'inherit', 'admin')
_MEMBER_KEYS = ('oid', 'set', 'inherit', 'admin')
_BINDING_KEYS = ('role_oid', 'role_name', 'logical_role', 'installation_id', 'active', 'revision', 'operation_id', 'database_oid', 'database_owner_oid', 'embedding_job_kind')


def _shape(ok):
    if not ok:
        raise RoleRefusal('catalog_shape')


def _dict(value, keys):
    _shape(type(value) is dict and set(value) == set(keys))
    return value


def _int(value):
    _shape(type(value) is int)
    return value


def _flags_of(value):
    flags = _dict(value, _FLAG_KEYS)
    _shape(all(type(flags[k]) is bool for k in _FLAG_KEYS))
    return flags


def _members(value):
    _shape(type(value) is list)
    out = []
    for edge in value:
        _dict(edge, _MEMBER_KEYS)
        _int(edge['oid'])
        _shape(all(type(edge[k]) is bool for k in ('set', 'inherit', 'admin')))
        out.append(dict(edge))
    return out


def validate_catalog(config, observation, *, phase='seed'):
    """Admit (a detached dict) or refuse (RoleRefusal catalog_*) one raw catalog observation for the seed phase: same policy and flags as render_*, the
    actual bootstrap session and database, five roles, the two fixed groups, EVERY edge incident on them (foreign incoming too), no service ownership of the
    database or protected objects, and bindings that are absent (fresh) or exactly the three revision-1 rows (replay). An observed revision is never copied
    as authority: the intent comes from INTENDED_REVISION."""
    if phase != 'seed':
        raise RoleRefusal('catalog_phase')
    try:
        if config.mode != 'seed':
            raise RoleRefusal('catalog_config')
        logins = dict(config.role_logins)
        installation, db_name = str(config.installation_id), config.database['name']
        _shape(set(logins) == set(A_PURPOSES) and all(type(v) is str for v in logins.values()))
    except (AttributeError, TypeError, KeyError):
        raise RoleRefusal('catalog_config') from None
    policy = load_roles()
    obs = _dict(observation, ('database', 'session', 'roles', 'groups', 'memberships', 'bindings', 'protected_owners'))
    db = _dict(obs['database'], ('name', 'oid', 'owner_oid'))
    _shape(type(db['name']) is str)
    _int(db['oid']), _int(db['owner_oid'])
    sess = _dict(obs['session'], ('login', 'oid', 'database_oid'))
    _shape(type(sess['login']) is str)
    _int(sess['oid']), _int(sess['database_oid'])
    raw_roles = obs['roles']
    if type(raw_roles) is not dict or set(raw_roles) != set(A_PURPOSES):
        raise RoleRefusal('catalog_roles')
    roles = {}
    for p in A_PURPOSES:
        r = _dict(raw_roles[p], ('login', 'oid', 'flags', 'memberships'))
        _shape(type(r['login']) is str)
        roles[p] = {'login': r['login'], 'oid': _int(r['oid']), 'flags': dict(_flags_of(r['flags'])), 'memberships': _members(r['memberships'])}
    raw_groups = obs['groups']
    if type(raw_groups) is not dict or set(raw_groups) != set(GROUPS):
        raise RoleRefusal('catalog_groups')
    groups = {}
    for g in GROUPS:
        r = _dict(raw_groups[g], ('login', 'oid', 'flags', 'memberships'))
        groups[g] = {'login': r['login'], 'oid': _int(r['oid']), 'flags': dict(_flags_of(r['flags'])), 'memberships': _members(r['memberships'])}
    _shape(type(obs['memberships']) is list and type(obs['protected_owners']) is list and type(obs['bindings']) is list)
    edges = []
    for e in obs['memberships']:
        _dict(e, _EDGE_KEYS)
        _int(e['member']), _int(e['oid'])
        _shape(all(type(e[k]) is bool for k in ('set', 'inherit', 'admin')))
        edges.append(dict(e))
    owners = []
    for o in obs['protected_owners']:
        _dict(o, ('kind', 'schema', 'name', 'owner_oid'))
        _shape(all(type(o[k]) is str for k in ('kind', 'schema', 'name')))
        owners.append({**o, 'owner_oid': _int(o['owner_oid'])})
    bindings = []
    for b in obs['bindings']:
        _dict(b, _BINDING_KEYS)
        _shape(all(type(b[k]) is str for k in ('role_name', 'logical_role', 'installation_id', 'operation_id'))
               and all(type(b[k]) is int for k in ('role_oid', 'revision', 'database_oid', 'database_owner_oid'))
               and type(b['active']) is bool and (b['embedding_job_kind'] is None or type(b['embedding_job_kind']) is str))
        bindings.append(dict(b))
    if db['name'] != db_name:
        raise RoleRefusal('catalog_database')
    if sess['login'] != logins['bootstrap'] or sess['oid'] != roles['bootstrap']['oid'] or sess['database_oid'] != db['oid']:
        raise RoleRefusal('catalog_session')
    oids = [roles[p]['oid'] for p in A_PURPOSES] + [groups[g]['oid'] for g in GROUPS]
    if any(roles[p]['login'] != logins[p] for p in A_PURPOSES) or len(set(oids)) != len(oids):
        raise RoleRefusal('catalog_roles')
    for p in A_PURPOSES:
        if roles[p]['flags'] != {k: policy[p][k] for k in _FLAG_KEYS}:
            raise RoleRefusal(f'catalog_flags:{p}')
    for g in GROUPS:
        if groups[g]['login'] != g or any(groups[g]['flags'].values()) or groups[g]['memberships']:
            raise RoleRefusal('catalog_groups')
    expected = sorted((roles[p]['oid'], groups[g]['oid'], True, False, False) for p in A_PURPOSES for g in policy[p]['memberships'])
    if sorted((e['member'], e['oid'], e['set'], e['inherit'], e['admin']) for e in edges) != expected:
        raise RoleRefusal('catalog_membership')
    for p in A_PURPOSES:  # each role's own outgoing list must be exactly its edges
        own = sorted((m['oid'], m['set'], m['inherit'], m['admin']) for m in roles[p]['memberships'])
        if own != sorted((e['oid'], e['set'], e['inherit'], e['admin']) for e in edges if e['member'] == roles[p]['oid']):
            raise RoleRefusal('catalog_membership')
    service = {roles[p]['oid'] for p in BOUND}
    if db['owner_oid'] in service or any(o['owner_oid'] in service for o in owners):
        raise RoleRefusal('catalog_ownership')
    if bindings:
        by_oid = {roles[p]['oid']: p for p in BOUND}
        seen = {}
        for b in bindings:
            p = by_oid.get(b['role_oid'])
            if (p is None or p in seen or b['role_name'] != roles[p]['login'] or b['logical_role'] != p or b['installation_id'] != installation
                    or not b['active'] or b['revision'] != INTENDED_REVISION[p] or b['database_oid'] != db['oid']
                    or b['database_owner_oid'] != db['owner_oid'] or b['embedding_job_kind'] is not None):
                raise RoleRefusal('catalog_bindings')
            seen[p] = b
        if set(seen) != set(BOUND) or len({b['operation_id'] for b in bindings}) != 1:
            raise RoleRefusal('catalog_bindings')
    return copy.deepcopy({'phase': phase, 'database': db, 'session': sess,
                          'roles': {p: {**roles[p], 'intended_revision': INTENDED_REVISION[p]} for p in A_PURPOSES},
                          'groups': {g: {'oid': groups[g]['oid']} for g in GROUPS},
                          'bindings': {'state': 'bound' if bindings else 'absent', 'rows': sorted(bindings, key=lambda b: b['role_oid'])},
                          'intended_revision': dict(INTENDED_REVISION)})
