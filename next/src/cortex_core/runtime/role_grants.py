"""Private finite PostgreSQL client, composed through the existing P2 primitive.

The installed owner remains unbound. A connection, role name, operation UUID or
retained reference alone never admits an effect. The fixed P2 composition owns
the adapter, its connections/material IO and the current consumed dispatch.
Nothing here changes the migration manifest or U13's journal.
"""
from contextlib import contextmanager
import json
from uuid import UUID

from psycopg import OperationalError, sql

from cortex_core.install import lifecycle, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw

__all__ = ['prepare_runtime_roles', 'bind_runtime_roles',
           'close_finite_roles', 'restore_runtime_roles']
_FIELDS = frozenset(('role', 'acl', 'database', 'hba', 'carrier'))
_SCHEMAS = ('core', 'auth', 'coordination', 'retrieval')
_SERVICES = ('api_request', 'probe', 'supervisor', 'observer', 'installer_enrollment')
_FLAGS = ('login', 'superuser', 'createdb', 'createrole', 'inherit', 'replication', 'bypassrls')
_SQL_FLAGS = ('LOGIN', 'SUPERUSER', 'CREATEDB', 'CREATEROLE', 'INHERIT', 'REPLICATION', 'BYPASSRLS')
_PRIVS = {'TABLE': ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'),
          'SEQUENCE': ('USAGE','SELECT','UPDATE'), 'FUNCTION': ('EXECUTE',),
          'TYPE': ('USAGE',), 'SCHEMA': ('USAGE','CREATE'), 'DATABASE': ('CONNECT','TEMPORARY','CREATE')}


def _require(value, reason='resource_changed'):
    owner._require(value, reason)


def _originals(history, context, *, expected_identity):
    """Resolve all original bytes before any write; never snapshot changed state."""
    c = thaw(context['context'])
    _require(lifecycle._operation(c['identity']) == lifecycle._operation(expected_identity), 'scope_denied')
    event, observations = lifecycle._load_entry(history, context['journal_ref'])
    _require(history.head()[0] == thaw(context['journal_ref']), 'reservation_used')
    _require(event['identity'] == c['identity'] and event['effect'] == c['effect']
             and event['request_ref'] == c['request_ref'], 'scope_denied')
    attempt = lifecycle._decode(history.read(observations['attempt_ref']))
    reservation = lifecycle._decode(history.read(observations['reservation_ref']))
    _require(attempt['dispatch_state'] == 'consumed_result_unknown'
             and reservation['state'] == 'consumed', 'reservation_used')
    ref = lifecycle._retained_preimages(history, c['identity'])
    _require(thaw(context['preimages_ref']) == observations['preimages_ref'] == ref, 'journal_invalid')
    bundle = lifecycle._decode(history.read(ref))
    _require(set(bundle) == {'identity','originals'} and set(bundle['originals']) == _FIELDS,
             'journal_invalid')
    # _History.read independently measures each retained P1 RecordRef.
    return {key: history.read(bundle['originals'][key]) for key in sorted(_FIELDS)}


def _run(action, conn, names, operation, parameters):
    try:
        return _run_bound(action,conn,names,operation,parameters)
    except owner.OwnerRefusal:
        raise
    except Exception:
        raise owner.OwnerRefusal('backend_unavailable') from None


def _run_bound(action, conn, names, operation, parameters):
    bound = lifecycle._installed_lifecycle()  # fixed owner BEFORE caller connection access
    _require(type(bound) is lifecycle._Bindings and type(bound.primitives) is dict, 'backend_unavailable')
    primitive = bound.primitives.get('database_role')
    _require(type(primitive) is _Primitive and primitive.client.history is bound.history, 'backend_unavailable')
    adapter = primitive.client
    _require(type(names) is dict and names == adapter.roles and str(operation) == adapter.identity['operation_id']
             and action == primitive.action and parameters == primitive.parameters
             and conn is primitive.connection,
             'scope_denied')
    inputs = lifecycle._owner_inputs()
    context = owner._decode(inputs.context_raw,'owner_unavailable')
    ref = context['request_ref']
    _require(ref['path'] in inputs.originals, 'journal_invalid')
    request = inputs.originals[ref['path']]
    lifecycle.execute_effect(request)
    return adapter.receipt()


def prepare_runtime_roles(conn, *, role_names, operation_id):
    return _run('prepare',conn,role_names,operation_id,{})


def bind_runtime_roles(conn, *, installation_id, role_names, operation_id, embedding_job_kind, activate):
    return _run('bind',conn,role_names,operation_id,dict(installation_id=installation_id,
                embedding_job_kind=embedding_job_kind,activate=activate))


def close_finite_roles(conn, *, role_names, operation_id, owned_backend_pids):
    return _run('close',conn,role_names,operation_id,dict(owned_backend_pids=owned_backend_pids))


def restore_runtime_roles(recovery_conn, *, role_names, operation_id, preimage):
    return _run('restore',recovery_conn,role_names,operation_id,dict(preimage=thaw(preimage)))


class _Primitive:
    """P2's original-preimage view of the same finite client.

    `client.snapshot()` always measures live state. P2's snapshot port supplies
    the FIRST operation originals on every phase, even after an earlier phase
    changed the live catalogue. Its independent measure port always rereads live
    state. No later snapshot can replace an operation's original preimages.
    """
    def __init__(self, client, action, connection, parameters):
        _require(type(client) is _RoleAdapter and action in ('prepare','bind','close','restore'), 'backend_unavailable')
        _require(connection is (client.recovery if action=='restore' else client.connection), 'scope_denied')
        self.client, self.action, self.connection = client, action, connection
        self.parameters = dict(parameters)

    def snapshot(self):
        live = self.client.snapshot()
        try:
            ref = lifecycle._retained_preimages(self.client.history,self.client.identity)
        except owner.OwnerRefusal as error:
            if error.detail != 'scope_denied':raise
            return live
        bundle = lifecycle._decode(self.client.history.read(ref))
        _require(set(bundle['originals']) == _FIELDS,'journal_invalid')
        return {k:self.client.history.read(bundle['originals'][k]) for k in sorted(_FIELDS)}

    def dispatch(self, context):
        a = self.client
        _require(a.context is None,'reservation_used')
        _originals(a.history,context,expected_identity=a.identity)
        a.context = context
        try:
            a.admit(self.connection)
            if self.action == 'bind':
                p = self.parameters
                _require(type(p['activate']) is bool and isinstance(p['installation_id'],UUID)
                         and p['installation_id']==a.installation_id and p['embedding_job_kind']==a.embedding_kind
                         and type(a.embedding_kind) is str and 0<len(a.embedding_kind)<=256,'scope_denied')
                a.bind(self.connection,p['activate'])
            elif self.action == 'close':
                pids = self.parameters['owned_backend_pids']
                _require(type(pids) in (tuple,list) and len(set(pids))==len(pids)
                         and set(pids)==set(a.backends),'scope_denied')
                a.close(self.connection)
            elif self.action == 'restore':
                _require(self.parameters['preimage']==lifecycle._retained_preimages(a.history,a.identity),'journal_invalid')
                a.restore(self.connection)
            else:a.prepare(self.connection)
            a._current()
        except BaseException:
            a.created.clear()
            raise
        finally:
            a.context = None

    def measure(self, context):
        try:
            return self.client.measure(context,restoring=self.action=='restore')
        finally:
            self.client.created.clear()


class _RoleAdapter:
    """ONE existing snapshot/dispatch/measure primitive; no public constructor.

    The protected installed composition must supply the actual policy, original
    connection/role/database bindings, owned backend lifetimes and material IO.
    _Primitive holds its compiled private finite action, not request data or an
    externally selected phase channel. Source fixtures can bind this privately;
    they do not confer native/installed owner qualification.
    """
    def __init__(self, *, history, identity, connection, recovery_connection, role_names,
                 installation_id, embedding_job_kind, policy, materials, backends):
        _require(type(history) is lifecycle._History and type(role_names) is dict
                 and set(role_names) == set(policy['roles']) and len(set(role_names.values())) == len(role_names),
                 'backend_unavailable')
        _require(all(type(v) is str and 0 < len(v.encode()) <= 63 and '\0' not in v for v in role_names.values()),
                 'backend_unavailable')
        _require(isinstance(installation_id,UUID) and str(installation_id)==identity['installation_id'],
                 'backend_unavailable')
        self.history, self.identity = history, thaw(identity)
        self.connection, self.recovery = connection, recovery_connection
        self.roles, self.policy = dict(role_names), thaw(policy)
        self.installation_id, self.embedding_kind = installation_id, embedding_job_kind
        self.materials, self.backends = materials, dict(backends)
        self.context = None
        self.created = {}
        self.database = self._database(recovery_connection)

    @staticmethod
    def _database(conn):
        return conn.execute('SELECT oid,datname,datdba,datallowconn,datconnlimit FROM pg_catalog.pg_database '
                            'WHERE datname=current_database()').fetchone()

    def _roles(self, conn):
        rows = conn.execute('SELECT oid,rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,'
                            'rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil::text,rolconfig '
                            'FROM pg_catalog.pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',
                            (list(self.roles.values()),)).fetchall()
        oids = [r[0] for r in rows]
        edges = conn.execute('SELECT roleid,member,grantor,admin_option,inherit_option,set_option '
                             'FROM pg_catalog.pg_auth_members WHERE roleid=ANY(%s) OR member=ANY(%s) '
                             'ORDER BY roleid,member,grantor', (oids,oids)).fetchall()
        return {'roles': [list(r) for r in rows], 'memberships': [list(r) for r in edges]}

    def _acl(self, conn):
        # ACL text is retained without normalizing NULL to effective defaults.
        rows = conn.execute("SELECT 'TABLE',c.oid,c.relowner,c.relacl::text[],"
                            "format('%%I.%%I',n.nspname,c.relname),c.relkind::text "
                            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','S','f') "
                            "UNION ALL SELECT 'FUNCTION',p.oid,p.proowner,p.proacl::text[],"
                            "p.oid::regprocedure::text,'' FROM pg_catalog.pg_proc p "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s) "
                            "UNION ALL SELECT 'TYPE',t.oid,t.typowner,t.typacl::text[],"
                            "format('%%I.%%I',n.nspname,t.typname),'' FROM pg_catalog.pg_type t "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
                            "WHERE n.nspname=ANY(%s) AND t.typtype IN ('d','e') "
                            "UNION ALL SELECT 'SCHEMA',oid,nspowner,nspacl::text[],quote_ident(nspname),'' "
                            "FROM pg_catalog.pg_namespace WHERE nspname=ANY(%s) "
                            "UNION ALL SELECT 'DATABASE',oid,datdba,datacl::text[],quote_ident(datname),'' "
                            "FROM pg_catalog.pg_database WHERE datname=current_database() ORDER BY 1,2",
                            (list(_SCHEMAS),list(_SCHEMAS),list(_SCHEMAS),[*_SCHEMAS,'public'])).fetchall()
        defaults = conn.execute('SELECT defaclrole,defaclnamespace,defaclobjtype::text,defaclacl::text[] '
                                'FROM pg_catalog.pg_default_acl WHERE defaclrole IN '
                                '(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) '
                                'ORDER BY defaclrole,defaclnamespace,defaclobjtype',
                                (list(self.roles.values()),)).fetchall()
        return {'objects':[list(r) for r in rows], 'defaults':[list(r) for r in defaults]}

    def snapshot(self):
        return self._snapshot(self.recovery)

    def _snapshot(self, conn):
        material = self.materials.snapshot()
        _require(type(material) is dict and set(material) == {'hba','carrier'}
                 and all(type(v) is bytes for v in material.values()), 'backend_unavailable')
        db = list(self._database(conn))
        _require(db[:2] == list(self.database[:2]))
        present = conn.execute("SELECT to_regclass('auth.runtime_login_bindings') IS NOT NULL").fetchone()[0]
        bindings = conn.execute('SELECT role_oid,role_name::text,logical_role,installation_id::text,active,revision,'
                                'operation_id::text,database_oid,database_owner_oid,embedding_job_kind '
                                'FROM auth.runtime_login_bindings ORDER BY role_oid').fetchall() if present else None
        return {'role':canonical_bytes(self._roles(conn)), 'acl':canonical_bytes(self._acl(conn)),
                'database':canonical_bytes({'database':db,'bindings':None if bindings is None else [list(r) for r in bindings]}),
                **material}

    def _current(self, context=None):
        context=self.context if context is None else context
        _require(context is not None, 'scope_denied')
        raw = _originals(self.history, context, expected_identity=self.identity)
        event, observation = lifecycle._load_entry(self.history, context['journal_ref'])
        inputs = lifecycle._owner_inputs()
        request = self.history.read(event['request_ref'])
        current = owner._evaluate(request, inputs)
        c = thaw(context['context'])
        lifecycle._continuity(c, thaw(current.context))
        _require(current.observed_state_revision == c['state_revision'], 'generation_changed')
        _require(c['effect']['resource_kind'] == 'database_role', 'scope_denied')
        reservation = lifecycle._decode(self.history.read(observation['reservation_ref']))
        clock = owner._decode(inputs.current_clock_raw, 'time_unavailable')
        lifecycle._before_deadline(clock, min(reservation['expires_at'],current.deadline.expires_at),
                                  min(reservation['expires_monotonic_ns'],current.deadline.expires_monotonic_ns))
        return raw

    def admit(self, conn):
        originals=self._current()
        _require(conn is self.connection or conn is self.recovery, 'scope_denied')
        db = self._database(conn)
        _require(db[:2] == self.database[:2])
        if conn is self.connection:_require(db[2] == self.database[2])
        original_db=lifecycle._decode(originals['database'])['database']
        _require(list(db[:2]) == original_db[:2])
        if conn is self.connection:_require(db[2] == original_db[2])
        login = conn.execute('SELECT a.usesysid,r.oid,r.rolname,current_user '
                             'FROM pg_catalog.pg_stat_activity a JOIN pg_catalog.pg_roles r '
                             'ON r.rolname=session_user WHERE a.pid=pg_backend_pid()').fetchone()
        _require(login and login[0] == login[1] and login[2] == login[3], 'scope_denied')
        if conn is self.connection:
            _require(login[2] == self.roles['bootstrap'], 'scope_denied')

    @contextmanager
    def _transaction(self, conn):
        self.admit(conn)
        with conn.transaction():
            conn.execute('SELECT pg_catalog.pg_advisory_xact_lock(%s)', (self.database[0],))
            self.admit(conn)
            yield
            self._current()  # refuse drift before transactional SQL publication

    def receipt(self):
        return {'operation_id':self.identity['operation_id'],
                'preimage':lifecycle._retained_preimages(self.history,self.identity)}

    def _known_oids(self, context=None):
        context=self.context if context is None else context
        original = lifecycle._decode(_originals(self.history,context,expected_identity=self.identity)['role'])
        known = {r[1]:r[0] for r in original['roles']}
        # Actual created OIDs are independently measured in the same P1 result;
        # a name-only current catalogue lookup cannot adopt a foreign role.
        for _, event in self.history.events():
            if lifecycle._operation(event['identity']) == lifecycle._operation(self.identity) and event['result_ref']:
                result = lifecycle._decode(self.history.read(event['result_ref']))
                if result['observed'].get('adapter') == 'finite_pg_roles':
                    measured = lifecycle._decode(self.history.read(result['observed']['originals']['role']))
                    for row in measured['roles']:
                        if row[1] in self.roles.values():known.setdefault(row[1],row[0])
        for name,oid in self.created.items():known.setdefault(name,oid)
        return known

    def prepare(self, conn):
        original = lifecycle._decode(self._current()['role'])
        expected = self._known_oids()
        with self._transaction(conn):
            current = {r[1]:r for r in self._roles(conn)['roles']}
            for logical,name in self.roles.items():
                row = current.get(name)
                if row:
                    _require(expected.get(name) == row[0])
                    if logical not in ('bootstrap','migrator'):
                        _require(not any(row[n] for n in (3,4,5,6,7,8)))
                        if logical in ('request_group','verifier_owner'):_require(not row[2])
                else:
                    _require(name not in expected)
                    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                         'NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(name)))
                    self.created[name] = conn.execute('SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',
                                                      (name,)).fetchone()[0]
        return self.receipt()

    def _objects(self):
        p = self.policy
        values = [('TABLE',row['name'],row['direct_privileges']) for row in p['tables']]
        values += [('FUNCTION',row['signature'],row['direct_privileges']) for row in p['functions']]
        for kind,key in [('TABLE','view'),('SEQUENCE','sequence'),('TYPE','type')]:
            values.append((kind,p[key]['name'],p[key]['direct_privileges']))
        return values

    def _plans(self):
        plans=[]
        for logical in _SERVICES:
            plans.append((logical,'DATABASE',self.database[1],['CONNECT']))
            for schema in (*_SCHEMAS,'public'):
                wanted=['USAGE'] if any(x.startswith('USAGE '+schema) for x in self.policy['schemas'].get(logical,[])) else []
                plans.append((logical,'SCHEMA',schema,wanted))
        plans.extend((logical,kind,name,rights[logical]) for kind,name,rights in self._objects() for logical in _SERVICES)
        _require(all(set(wanted)<=set(_PRIVS[kind]) for _,kind,_,wanted in plans),'backend_unavailable')
        return plans

    def _privileges(self, conn, plans):
        cells=[(logical,kind,name,privilege,privilege in wanted)
               for logical,kind,name,wanted in plans for privilege in _PRIVS[kind]]
        expressions=[]
        for option in (''," || ' WITH GRANT OPTION'"):
            expressions.append('CASE q.kind '+ ' '.join(
                "WHEN '"+kind+"' THEN pg_catalog.has_"+fn+"_privilege(q.role_name,q.object_name,q.privilege"+option+")"
                for kind,fn in [('TABLE','table'),('SEQUENCE','sequence'),('FUNCTION','function'),
                                ('TYPE','type'),('SCHEMA','schema'),('DATABASE','database')])+ ' END')
        query='SELECT q.n,'+','.join(expressions)+' FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[]) WITH ORDINALITY AS q(role_name,kind,object_name,privilege,n) ORDER BY q.n'
        rows=conn.execute(query,([self.roles[c[0]] for c in cells],[c[1] for c in cells],
                                 [c[2] for c in cells],[c[3] for c in cells])).fetchall()
        _require(len(rows)==len(cells) and [r[0] for r in rows]==list(range(1,len(cells)+1)))
        return [(cell,row[1],row[2]) for cell,row in zip(cells,rows)]

    def _apply_grants(self, conn, plans):
        for (logical,kind,name,privilege,want),actual,grant_option in self._privileges(conn,plans):
            if actual != want or grant_option:
                verb='GRANT '+privilege+' ON '+kind+' {} TO {}' if want else 'REVOKE '+privilege+' ON '+kind+' {} FROM {}'
                target=sql.Identifier(name) if kind in ('DATABASE','SCHEMA') else sql.SQL(name)
                conn.execute(sql.SQL(verb).format(target,sql.Identifier(self.roles[logical])))
                if grant_option:
                    conn.execute(sql.SQL('REVOKE GRANT OPTION FOR '+privilege+' ON '+kind+' {} FROM {} CASCADE')
                                 .format(target,sql.Identifier(self.roles[logical])))

    def validate_acl(self, conn):
        for (_,_,_,_,want),actual,grant_option in self._privileges(conn,self._plans()):
            _require(actual==want and not grant_option)
        acl = self._acl(conn)
        expected_objects = {('FUNCTION',row['signature']) for row in self.policy['functions']}
        expected_objects |= {('TABLE',row['name']) for row in self.policy['tables']}
        expected_objects |= {('TABLE',self.policy[k]['name']) for k in ('view','sequence','control_ledger')}
        expected_objects |= {('TYPE',self.policy['type']['name'])}
        expected_oids=set()
        for kind,name in expected_objects:
            resolver={'FUNCTION':'to_regprocedure','TYPE':'to_regtype','TABLE':'to_regclass'}[kind]
            oid=conn.execute(sql.SQL('SELECT pg_catalog.'+resolver+'(%s)::oid'),(name,)).fetchone()[0]
            _require(oid is not None)
            expected_oids.add((kind,oid))
        actual_objects = {(kind,oid) for kind,oid,_,_,_,_ in acl['objects'] if kind not in ('SCHEMA','DATABASE')}
        _require(actual_objects == expected_oids)
        for kind,oid,object_owner,entries,_,relkind in acl['objects']:
            if kind == 'SCHEMA':continue
            acl_kind = {'TABLE':'s' if relkind=='S' else 'r','FUNCTION':'f','TYPE':'T','DATABASE':'d'}[kind]
            _require(not conn.execute('SELECT 1 FROM pg_catalog.aclexplode('
                                      'COALESCE(%s::aclitem[],pg_catalog.acldefault(%s::"char",%s::oid))) WHERE grantee=0',
                                      (entries,acl_kind,object_owner)).fetchone())
        for creator,_,_,entries in acl['defaults']:
            _require(not conn.execute('SELECT 1 FROM pg_catalog.aclexplode(%s::aclitem[]) WHERE grantee=0',
                                      (entries,)).fetchone())
        for logical in ('bootstrap','migrator'):
            creator=conn.execute('SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',(self.roles[logical],)).fetchone()[0]
            for kind in ('f','T'):
                rows=[r[3] for r in acl['defaults'] if r[:3]==[creator,0,kind]]
                _require(len(rows)<=1)
                _require(not conn.execute('SELECT 1 FROM pg_catalog.aclexplode('
                    'COALESCE(%s::aclitem[],pg_catalog.acldefault(%s::"char",%s::oid))) WHERE grantee=0',
                    (rows[0] if rows else None,kind,creator)).fetchone())
        nonfinite=[v for k,v in self.roles.items() if k not in ('bootstrap','migrator')]
        _require(not conn.execute('SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba IN '
            '(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) OR '
            '(d.oid<>%s AND EXISTS(SELECT 1 FROM pg_catalog.aclexplode(d.datacl) a '
            'WHERE a.grantee IN (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)))) LIMIT 1',
            (nonfinite,self.database[0],nonfinite)).fetchone())
        _require(self._roles(conn)['memberships'] == self._expected_membership(conn))

    def _expected_membership(self, conn):
        row = conn.execute('SELECT g.oid,m.oid FROM pg_catalog.pg_roles g,pg_catalog.pg_roles m '
                           'WHERE g.rolname=%s AND m.rolname=%s',
                           (self.roles['request_group'],self.roles['api_request'])).fetchone()
        # The grantor is retained and checked separately by the current exact
        # graph; no ADMIN/INHERIT or role-edge expansion is permitted.
        edges = self._roles(conn)['memberships']
        _require(len(edges)==1 and edges[0][:2]==list(row) and edges[0][3:]==[False,False,True])
        return edges

    def bind(self, conn, activate):
        known = self._known_oids()
        with self._transaction(conn):
            roles = {r[1]:r[0] for r in self._roles(conn)['roles']}
            _require(all(name in roles and known.get(name)==roles[name] for name in self.roles.values()))
            for row in self._roles(conn)['roles']:
                logical=next(k for k,v in self.roles.items() if v==row[1])
                if logical not in ('bootstrap','migrator'):
                    _require(not any(row[n] for n in (3,4,5,6,7,8)))
                    if logical in ('request_group','verifier_owner'):_require(not row[2])
            # Lock the existing protected map in a stable order, before changes.
            old = conn.execute('SELECT role_oid,role_name::text,logical_role,installation_id,active,revision,'
                               'operation_id,database_oid,database_owner_oid,embedding_job_kind '
                               'FROM auth.runtime_login_bindings ORDER BY role_oid FOR UPDATE').fetchall()
            for row in old:
                _require(row[1] in self.roles.values() and str(row[3])==str(self.installation_id)
                         and str(row[6])==self.identity['operation_id'] and row[7:9]==self.database[::2][:2])
            for logical in _SERVICES:
                name = self.roles[logical]
                conn.execute('INSERT INTO auth.runtime_login_bindings '
                             '(role_oid,role_name,logical_role,installation_id,active,revision,operation_id,'
                             'database_oid,database_owner_oid,embedding_job_kind) '
                             'VALUES (%s,%s,%s,%s,false,1,%s,%s,%s,%s) ON CONFLICT (role_oid) DO NOTHING',
                             (roles[name],name,logical,self.installation_id,UUID(self.identity['operation_id']),
                              self.database[0],self.database[2],self.embedding_kind if logical=='observer' else None))
                bound = conn.execute('SELECT role_name::text,logical_role,installation_id,operation_id,'
                                     'database_oid,database_owner_oid,embedding_job_kind FROM auth.runtime_login_bindings '
                                     'WHERE role_oid=%s', (roles[name],)).fetchone()
                _require(bound==(name,logical,self.installation_id,UUID(self.identity['operation_id']),
                                  self.database[0],self.database[2],self.embedding_kind if logical=='observer' else None))
            edges = self._roles(conn)['memberships']
            group,member = roles[self.roles['request_group']],roles[self.roles['api_request']]
            _require(all(edge[:2]==[group,member] for edge in edges))
            if len(edges)!=1 or edges[0][3:]!=[False,False,True]:
                for edge in edges:
                    grantor = conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(edge[2],)).fetchone()[0]
                    conn.execute(sql.SQL('REVOKE {} FROM {} GRANTED BY {}')
                                 .format(sql.Identifier(self.roles['request_group']),sql.Identifier(self.roles['api_request']),sql.Identifier(grantor)))
                conn.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE,SET TRUE,ADMIN FALSE')
                             .format(sql.Identifier(self.roles['request_group']),sql.Identifier(self.roles['api_request'])))
            self._apply_grants(conn,self._plans())
            self.validate_acl(conn)
            if activate:
                for logical in _SERVICES:
                    conn.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(self.roles[logical])))
                conn.execute('UPDATE auth.runtime_login_bindings SET active=true,revision=revision+1 '
                             'WHERE operation_id=%s AND NOT active', (UUID(self.identity['operation_id']),))
        return self.receipt()

    def close(self, conn):
        finite=[self.roles['bootstrap'],self.roles['migrator']]
        query=('SELECT pid,usesysid,backend_start::text,datid FROM pg_catalog.pg_stat_activity '
               'WHERE usesysid IN (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) ORDER BY pid')
        current = conn.execute(query,(finite,)).fetchall()
        _require({r[0]:tuple(r[1:]) for r in current} == self.backends, 'scope_denied')
        self_pid = conn.info.backend_pid
        _require(self_pid in self.backends, 'scope_denied')
        with self._transaction(conn):
            # The database lock can wait. A PID set measured before that wait
            # cannot authorize terminating a different backend lifetime.
            _require({r[0]:tuple(r[1:]) for r in conn.execute(query,(finite,)).fetchall()} == self.backends,
                     'scope_denied')
            for pid,lifetime in self.backends.items():
                if pid != self_pid:
                    row=conn.execute('SELECT pg_catalog.pg_terminate_backend(pid,1000) '
                                     'FROM pg_catalog.pg_stat_activity WHERE pid=%s AND usesysid=%s '
                                     'AND backend_start::text=%s AND datid=%s',(pid,*lifetime)).fetchone()
                    _require(row and row[0])
            for logical in ('migrator','bootstrap'):
                conn.execute(sql.SQL('ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                     'NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(self.roles[logical])))
        # The original finite caller closes LAST. Independent recovery IO, not
        # a lost socket, decides whether all exact owned backend lifetimes drain.
        try:
            conn.execute('SELECT pg_catalog.pg_terminate_backend(pg_backend_pid())')
        except OperationalError:
            if self.recovery.execute(query,(finite,)).fetchall():
                raise
        _require(not self.recovery.execute(query,(finite,)).fetchall())
        return self.receipt()

    def _restore_acl(self, conn, original):
        current = self._acl(conn)
        self._restore_defaults(conn,original['defaults'],current['defaults'])
        observed = {(r[0],r[1]):r for r in current['objects']}
        _require(set(observed) == {(r[0],r[1]) for r in original['objects']})
        # Preflight all identities/NULL states before the first ACL effect.
        for row in original['objects']:
            actual = observed[(row[0],row[1])]
            _require(actual[2] == row[2] and actual[4:] == row[4:])
            if row[3] is None:_require(actual[3] is None)
        for kind,oid,_,entries,name,relkind in original['objects']:
            actual = observed[(kind,oid)]
            if entries == actual[3]:continue
            target_kind = 'SEQUENCE' if relkind=='S' else kind
            grantees = conn.execute('SELECT DISTINCT grantee FROM pg_catalog.aclexplode(%s::aclitem[])',
                                    (actual[3],)).fetchall()
            for (grantee,) in grantees:
                role = sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                conn.execute(sql.SQL('REVOKE ALL ON '+target_kind+' {} FROM {} CASCADE').format(sql.SQL(name),role))
            rows = conn.execute('SELECT grantor,grantee,privilege_type,is_grantable '
                                'FROM unnest(%s::aclitem[]) WITH ORDINALITY a(item,n) '
                                'CROSS JOIN LATERAL pg_catalog.aclexplode(ARRAY[a.item]) ORDER BY n', (entries,)).fetchall()
            for grantor,grantee,privilege,option in rows:
                _require(privilege in _PRIVS[target_kind])
                recipient = sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                giver = sql.Identifier(conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantor,)).fetchone()[0])
                # Object GRANTED BY must be the current user, including for a
                # superuser recovery connection. Preserve the original grantor
                # through PostgreSQL's checked SET ROLE, never a catalogue edit.
                conn.execute(sql.SQL('SET LOCAL ROLE {}').format(giver))
                try:
                    conn.execute(sql.SQL('GRANT '+privilege+' ON '+target_kind+' {} TO {}'+(' WITH GRANT OPTION' if option else '')+' GRANTED BY {}')
                                 .format(sql.SQL(name),recipient,giver))
                finally:
                    conn.execute('RESET ROLE')

    def _restore_defaults(self, conn, original, current):
        before={tuple(row[:3]):row[3] for row in original}
        now={tuple(row[:3]):row[3] for row in current}
        kinds={'f':('FUNCTIONS','FUNCTION'),'T':('TYPES','TYPE'),'r':('TABLES','TABLE'),
               'S':('SEQUENCES','SEQUENCE'),'n':('SCHEMAS','SCHEMA')}
        for key in sorted(set(before)|set(now)):
            if before.get(key)==now.get(key):continue
            creator,namespace,kind=key
            _require(kind in kinds)
            creator_name=conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(creator,)).fetchone()[0]
            _require(creator_name in self.roles.values())
            prefix=sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {}').format(sql.Identifier(creator_name))
            if namespace:
                name=conn.execute('SELECT nspname FROM pg_catalog.pg_namespace WHERE oid=%s',(namespace,)).fetchone()
                _require(name and name[0] in (*_SCHEMAS,'public'))
                prefix += sql.SQL(' IN SCHEMA {}').format(sql.Identifier(name[0]))
            for (grantee,) in conn.execute('SELECT DISTINCT grantee FROM pg_catalog.aclexplode(%s::aclitem[])',
                                           (now.get(key),)).fetchall():
                recipient=sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                conn.execute(prefix+sql.SQL(' REVOKE ALL ON '+kinds[kind][0]+' FROM {} CASCADE').format(recipient))
            target=before.get(key)
            if target is None:
                target=conn.execute('SELECT pg_catalog.acldefault(%s::"char",%s::oid)::text[]',
                                    (kind,creator)).fetchone()[0] if namespace==0 else []
            rows=conn.execute('SELECT grantor,grantee,privilege_type,is_grantable '
                'FROM unnest(%s::aclitem[]) WITH ORDINALITY a(item,n) '
                'CROSS JOIN LATERAL pg_catalog.aclexplode(ARRAY[a.item]) ORDER BY n',(target,)).fetchall()
            for grantor,grantee,privilege,option in rows:
                _require(grantor==creator and privilege in _PRIVS[kinds[kind][1]])
                recipient=sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                conn.execute(prefix+sql.SQL(' GRANT '+privilege+' ON '+kinds[kind][0]+' TO {}'+(' WITH GRANT OPTION' if option else '')).format(recipient))

    def restore(self, conn):
        originals = self._current()  # read/measure ALL five first
        roles = lifecycle._decode(originals['role'])
        database = lifecycle._decode(originals['database'])
        acl = lifecycle._decode(originals['acl'])
        known = self._known_oids()
        with self._transaction(conn):
            current = {r[1]:r for r in self._roles(conn)['roles']}
            for row in roles['roles']:_require(row[1] in current and current[row[1]][0]==row[0])
            for name,row in current.items():_require(known.get(name)==row[0])
            db = database['database'];_require(list(self._database(conn))[:2] == db[:2])
            db_owner = conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(db[2],)).fetchone()[0]
            _require(known.get(db_owner)==db[2])
            conn.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}')
                         .format(sql.Identifier(db[1]),sql.Identifier(db_owner)))
            self._restore_acl(conn,acl)
            for row in roles['roles']:
                # PostgreSQL's ALTER ROLE does not encode NULL as infinity;
                # preserve the original representation rather than relabel it.
                if row[10] is None:_require(current[row[1]][10] is None)
                flags = ' '.join(word if value else 'NO'+word for word,value in zip(_SQL_FLAGS,row[2:9]))
                conn.execute(sql.SQL('ALTER ROLE {} '+flags+' CONNECTION LIMIT {}')
                             .format(sql.Identifier(row[1]),sql.Literal(row[9])))
                if row[10] != current[row[1]][10]:
                    conn.execute(sql.SQL('ALTER ROLE {} VALID UNTIL {}')
                                 .format(sql.Identifier(row[1]),sql.Literal(row[10])))
                if current[row[1]][11] != row[11]:
                    conn.execute(sql.SQL('ALTER ROLE {} RESET ALL').format(sql.Identifier(row[1])))
                    for setting in row[11] or []:
                        key,value=setting.split('=',1)
                        conn.execute(sql.SQL('ALTER ROLE {} SET {} TO {}')
                                     .format(sql.Identifier(row[1]),sql.Identifier(key),sql.Literal(value)))
            edges = self._roles(conn)['memberships']
            for granted,member,grantor,admin,inherit,set_role in edges:
                if [granted,member,grantor,admin,inherit,set_role] not in roles['memberships']:
                    names = [conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(oid,)).fetchone()[0]
                             for oid in (granted,member)]
                    _require(all(name in self.roles.values() for name in names))
                    grantor_name=conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(grantor,)).fetchone()[0]
                    conn.execute(sql.SQL('REVOKE {} FROM {} GRANTED BY {}')
                                 .format(*map(sql.Identifier,names),sql.Identifier(grantor_name)))
            for granted,member,grantor,admin,inherit,set_role in roles['memberships']:
                if [granted,member,grantor,admin,inherit,set_role] not in edges:
                    names = [conn.execute('SELECT pg_catalog.pg_get_userbyid(%s)',(oid,)).fetchone()[0]
                             for oid in (granted,member,grantor)]
                    conn.execute(sql.SQL('GRANT {} TO {} WITH ADMIN {},INHERIT {},SET {} GRANTED BY {}')
                                 .format(sql.Identifier(names[0]),sql.Identifier(names[1]),sql.Literal(admin),
                                         sql.Literal(inherit),sql.Literal(set_role),sql.Identifier(names[2])))
            conn.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS {} CONNECTION LIMIT {}')
                         .format(sql.Identifier(db[1]),sql.Literal(db[3]),sql.Literal(db[4])))
            if database['bindings'] is not None:
                conn.execute('DELETE FROM auth.runtime_login_bindings WHERE operation_id=%s', (UUID(self.identity['operation_id']),))
                for row in database['bindings']:
                    conn.execute('INSERT INTO auth.runtime_login_bindings VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',row)
            original_names = {r[1] for r in roles['roles']}
            for name in sorted(set(current)-original_names):
                conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
            self.materials.restore({'hba':originals['hba'],'carrier':originals['carrier']})
            _require(self._snapshot(conn) == originals)  # same SQL transaction; P2 measure rereads after COMMIT
        return self.receipt()

    def measure(self, context, *, restoring=False):
        # P2's dispatcher independently rereads after the compiled finite work.
        c = thaw(context['context'])
        raw = self.snapshot()
        originals=_originals(self.history,context,expected_identity=self.identity)
        known=self._known_oids(context)
        _require(all(known.get(row[1])==row[0] for row in lifecycle._decode(raw['role'])['roles']))
        if restoring:_require(raw==originals)
        refs={k:self.history.retain(v) for k,v in raw.items()}
        self._current(context)  # final owner/deadline check after independent measurement IO
        return canonical_bytes({'identity':c['identity'],'effect':c['effect'],
            'request_ref':c['request_ref'],'state':'applied_verified',
            'observed':{'adapter':'finite_pg_roles','originals':refs}})
