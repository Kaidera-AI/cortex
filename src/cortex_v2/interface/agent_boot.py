"""Read-only boot snapshots from credential-bound canonical rows."""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from ..agent_boot import select_boot_heads
from ..store import ApiProblem


def unavailable():
    return ApiProblem(409, 'boot_binding_unavailable', 'Registered own-agent boot facts are unavailable.')


def object_value(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise unavailable()
    return value


def canonical(row):
    result = dict(row)
    if result.get('boot_manifest') is not None:
        result['boot_manifest'] = object_value(result['boot_manifest'])
    return result


async def load_snapshot(connection, context):
    scope = context.selected
    principal = context.principal
    if scope.kind != 'project' or not scope.can_read or context.read_scopes != (scope,):
        raise unavailable()
    configured = await connection.fetchrow("""SELECT
        current_setting('cortex.principal_id',true) AS principal,
        current_setting('cortex.read_scope_ids',true) AS scopes,
        cortex_context.boot_project_reader($1) AS eligible""", principal.installation_id)
    if (configured['principal'] != str(principal.principal_id)
            or configured['scopes'] != str(scope.scope_id) or not configured['eligible']):
        raise unavailable()
    actors = await connection.fetch("""SELECT actor.actor_id, actor.display_name
        FROM cortex_auth.actor_bindings AS binding
        JOIN cortex_auth.actors AS actor ON actor.actor_id=binding.actor_id
        JOIN cortex_auth.memberships AS member ON member.actor_id=actor.actor_id
        WHERE binding.principal_id=$1 AND actor.installation_id=$2
          AND actor.actor_kind='agent' AND actor.status='active'
          AND member.scope_id=$3 AND member.status='active'""",
        principal.principal_id, principal.installation_id, scope.scope_id)
    if len(actors) != 1:
        raise unavailable()
    actor = actors[0]
    agents = [dict(row) for row in await connection.fetch(
        'SELECT * FROM cortex_context.boot_agent_binding_revisions WHERE project_scope_id=$1', scope.scope_id)]
    head = select_boot_heads(agents, stream='agent').get((scope.scope_id, actor['actor_id']))
    if head is None or head['state'] != 'active':
        raise unavailable()
    identity = await connection.fetchrow("""SELECT identity_name, original_record
        FROM cortex_core.project_identities WHERE project_scope_id=$1 AND identity_id=$2 AND identity_kind='agent'""",
        scope.scope_id, head['identity_id'])
    registry = await connection.fetchrow(
        'SELECT repo_root,status FROM cortex_core.project_registry WHERE project_scope_id=$1', scope.scope_id)
    if identity is None or identity['identity_name'] != actor['display_name'] or registry is None or registry['status'] != 'active':
        raise unavailable()
    facts = object_value(identity['original_record'])
    roles = facts.get('functional_roles')
    role = facts.get('role')
    if roles is None and isinstance(role, str):
        roles = [role]
    if (not isinstance(roles, list) or not roles or len(roles) > 64
            or any(not isinstance(value, str) or not value or '\x00' in value for value in roles)
            or len(roles) != len(set(roles)) or set(roles) != set(head['functional_roles'])):
        raise unavailable()
    if role is None and len(roles) == 1:
        role = roles[0]
    if role not in roles:
        raise unavailable()
    root = registry['repo_root']
    path = PurePosixPath(root)
    if (not path.is_absolute() or str(path) != root or '..' in path.parts or root == '/'
            or '\\' in root or any(ord(char) < 32 or ord(char) == 127 for char in root)):
        raise unavailable()
    personas = [canonical(row) for row in await connection.fetch("""SELECT scope_id,persona_id,revision,body,audience,boot_manifest
        FROM cortex_context.persona_revisions WHERE scope_id=$1 AND persona_id=$2 AND revision=$3""",
        scope.scope_id, head['persona_id'], head['persona_revision'])]
    if len(personas) != 1 or personas[0]['boot_manifest'] is None:
        raise unavailable()
    profiles = await connection.fetch("""SELECT original_record FROM cortex_core.project_profiles
        WHERE project_scope_id=$1 AND agent_name=$2""",scope.scope_id,actor['display_name'])
    if not any((profile := object_value(row['original_record'])).get('profile_kind') == 'identity'
               and profile.get('profile_text') == personas[0]['boot_manifest'].get('identity_text') for row in profiles):
        raise unavailable()
    entries = [dict(row) for row in await connection.fetch(
        'SELECT * FROM cortex_context.boot_entry_binding_revisions WHERE project_scope_id=$1', scope.scope_id)]
    publications = [dict(row) for row in await connection.fetch(
        'SELECT * FROM cortex_context.boot_catalogue_publication_revisions WHERE installation_id=$1', principal.installation_id)]
    eligible = set()
    for row in select_boot_heads(publications, stream='publication').values():
        if await connection.fetchval('SELECT cortex_context.boot_publication_eligible($1,$2,$3)',
                                     row['installation_id'],row['publication_id'],row['revision']):
            eligible.add((row['installation_id'],row['publication_id'],row['revision']))
    rows = {}
    for kind in ('skill', 'rule'):
        # Kind is a fixed internal enumerated name, never a request identifier.
        rows[kind] = [canonical(row) for row in await connection.fetch(
            f'''SELECT * FROM cortex_context.{kind}_revisions WHERE scope_id=$1
                 OR cortex_context.boot_published_revision('{kind}',scope_id,{kind}_id,revision)''', scope.scope_id)]
    return {'context': {'project_scope_id':scope.scope_id, 'installation_id':principal.installation_id,
            'actor_id':actor['actor_id'], 'actor_name':actor['display_name'], 'actor_role':role,
            'functional_roles':roles, 'project_key':scope.alias, 'workspace_root':root,
            'eligible_publication_heads':eligible},
        'agent_rows':agents,'entry_rows':entries,'publication_rows':publications,
        'persona_rows':personas,'skill_rows':rows['skill'],'rule_rows':rows['rule'],
        'operational':{'projection_available':False,'boot_tail':'','boot_context':{'counts':{},'work_products':[]},
                       'pending_handoffs':[],'harness':{'state':'unavailable','reason':'projection_unavailable'},
                       'surface_version':'kaidera-os-e009-clean-baseline-2026-06-24'}}
