"""Actual read-only catalog observations; Nix alone owns admission policy."""
from .signed_member import Refusal

_FLAGS=('login','superuser','createdb','createrole','inherit','replication','bypassrls')
_GROUPS=('kaidera-runtime-core-request','kaidera-runtime-core-verifier')
_SCHEMAS=('core','auth','coordination','retrieval')


def _rows(connection,sql,parameters=None):
    return connection.execute(sql,parameters).fetchall()


def observe(resources):
    # No public snapshot/connection argument is used by the production accessor.
    from .resources import current_seed_resources
    current_seed_resources(resources._config,resources._deadline)
    return _observe(resources,service=False)


def observe_service(resources):
    from .resources import CoreResources
    if type(resources) is not CoreResources:raise Refusal('resources_unbound')
    resources.check_deadline()
    return _observe(resources,service=True)


def _observe(resources,*,service):
    try:
        purpose='api_request' if service else 'bootstrap'
        with resources.transaction(purpose,_read_only=True,_repeatable=not service) as connection:
            databases=_rows(connection,'''SELECT datname,oid::bigint,datdba::bigint
                FROM pg_catalog.pg_database WHERE datname=current_database()''')
            sessions=_rows(connection,'''SELECT session_user,usesysid::bigint,datid::bigint
                FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid()''')
            if len(databases)!=1 or len(sessions)!=1:raise ValueError
            database=dict(zip(('name','oid','owner_oid'),databases[0],strict=True))
            session=dict(zip(('login','oid','database_oid'),sessions[0],strict=True))
            names=list(resources.role_logins.values())+list(_GROUPS)
            rows=_rows(connection,'''SELECT rolname,oid::bigint,rolcanlogin,rolsuper,
                rolcreatedb,rolcreaterole,rolinherit,rolreplication,rolbypassrls
                FROM pg_catalog.pg_roles WHERE rolname=ANY(%s) ORDER BY oid''',(names,))
            by_name={}
            for row in rows:
                login,oid,*flags=row
                if login in by_name or login not in names or len(flags)!=len(_FLAGS):raise ValueError
                if type(oid) is not int or not all(type(flag) is bool for flag in flags):raise ValueError
                by_name[login]=dict(login=login,oid=oid,flags=dict(zip(_FLAGS,flags,strict=True)),memberships=[])
            oids=[row['oid'] for row in by_name.values()]
            edges=_rows(connection,'''SELECT member::bigint,roleid::bigint,
                set_option,inherit_option,admin_option
                FROM pg_catalog.pg_auth_members
                WHERE member=ANY(%s::oid[]) OR roleid=ANY(%s::oid[])
                ORDER BY member,roleid''',(oids,oids))
            memberships=[]
            for row in edges:
                edge=dict(zip(('member','oid','set','inherit','admin'),row,strict=True));memberships.append(edge)
                for role in by_name.values():
                    if edge['member']==role['oid']:
                        role['memberships'].append({key:edge[key] for key in ('oid','set','inherit','admin')})
            bindings=None
            if not service:
                binding_rows=_rows(connection,'''SELECT role_oid::bigint,role_name::text,
                logical_role,installation_id::text,active,revision,operation_id::text,
                database_oid::bigint,database_owner_oid::bigint,embedding_job_kind
                FROM auth.runtime_login_bindings
                WHERE installation_id=%s::uuid OR role_oid=ANY(%s::oid[])
                    OR role_name=ANY(%s::name[]) ORDER BY role_oid''',
                (resources._config.installation_id,oids,list(resources.role_logins.values())))
                keys=('role_oid','role_name','logical_role','installation_id','active','revision',
                    'operation_id','database_oid','database_owner_oid','embedding_job_kind')
                bindings=[dict(zip(keys,row,strict=True)) for row in binding_rows]
            owners=_rows(connection,'''SELECT 'schema',nspname,nspname,nspowner::bigint
                FROM pg_catalog.pg_namespace WHERE nspname=ANY(%s)
                UNION ALL SELECT 'relation',n.nspname,c.relname,c.relowner::bigint
                FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=ANY(%s) ORDER BY 1,2,3''',(list(_SCHEMAS),list(_SCHEMAS)))
            protected=[dict(zip(('kind','schema','name','owner_oid'),row,strict=True)) for row in owners]
            observation=dict(database=database,session=session,
                roles={purpose:by_name[login] for purpose,login in resources.role_logins.items() if login in by_name},
                groups={name:by_name[name] for name in _GROUPS if name in by_name},
                memberships=memberships,bindings=bindings,protected_owners=protected)
            if service:
                connection.execute('SET LOCAL ROLE "kaidera-runtime-core-request"')
                installation=_rows(connection,'SELECT auth.runtime_request_installation(%s::uuid)',
                    (resources._config.installation_id,))
                enrollment=_rows(connection,'SELECT auth.enrollment_readiness(%s::uuid)::text',
                    (resources._config.installation_id,))
                if (len(installation)!=1 or len(installation[0])!=1 or type(installation[0][0]) is not bool
                        or len(enrollment)!=1 or len(enrollment[0])!=1
                        or enrollment[0][0] not in ('ready','enrollment_required','unavailable')):raise ValueError
                observation['service']=dict(installation_matches=installation[0][0],enrollment_readiness=enrollment[0][0])
            return observation
    except Refusal as error:
        if error.reason in ('resources_deadline','resources_closed','resources_unbound'):raise
        raise Refusal('catalog_unavailable') from None
    except Exception:raise Refusal('catalog_unavailable') from None
