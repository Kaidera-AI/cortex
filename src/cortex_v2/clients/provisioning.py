"""Explicit Linux fresh-project policy; native effects are declared ports."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import re

from .native_prerequisite import PrerequisiteRefusal, canonical_uuid, physical_path, IDENTIFIER


class ProvisionRefusal(PrerequisiteRefusal):
    pass


REQUEST_FIELDS = {'source_project', 'project_key', 'display_name', 'lead_name', 'lead_responsibility',
    'lead_key_manager', 'with_console', 'parent_project_key', 'repo_root', 'roots'}
ARGUMENT_FIELDS = {'mode', 'host_os', 'create_project', 'idempotency_key', 'operation_id',
                   'operator_explicit', 'manager_explicit'}


def validate_request(args: dict) -> dict:
    try:
        if (not isinstance(args, dict) or not set(args) <= ARGUMENT_FIELDS
                or args['mode'] != 'create-project' or args['host_os'] != 'linux'
                or args['operator_explicit'] is not True or args['manager_explicit'] is not True
                or not isinstance(args['idempotency_key'], str)
                or not re.fullmatch(r'[\x21-\x7e]{1,128}', args['idempotency_key'])
                or len(json.dumps(args, ensure_ascii=False, allow_nan=False).encode()) > 65536):
            raise ValueError
        body = args['create_project']
        if (not isinstance(body, dict) or set(body) != REQUEST_FIELDS or body['source_project'] is not None
                or body['with_console'] is not True or body['lead_key_manager'] not in ('kos', 'openkai', 'user')
                or not IDENTIFIER.fullmatch(body['project_key']) or not IDENTIFIER.fullmatch(body['lead_name'])
                or body['lead_name'].casefold() in ('console', 'owner', 'recovery')):
            raise ValueError
        for field in ('display_name', 'lead_name', 'lead_responsibility'):
            if not isinstance(body[field], str) or not 1 <= len(body[field]) <= 128 or '\x00' in body[field]:
                raise ValueError
        if body['parent_project_key'] is not None and not IDENTIFIER.fullmatch(body['parent_project_key']):
            raise ValueError
        root = Path(body['repo_root']); physical_path(root)
        if not root.is_dir(): raise ValueError
        roots = body['roots']
        if not isinstance(roots, list) or not 1 <= len(roots) <= 64:
            raise ValueError
        primary = []
        for item in roots:
            if not isinstance(item, dict) or item['kind'] not in ('primary', 'reference'):
                raise ValueError
            path = Path(item['path']); physical_path(path)
            if not path.is_dir(): raise ValueError
            if item['kind'] == 'primary': primary.append(item['path'])
        if primary != [body['repo_root']]: raise ValueError
        return body
    except (KeyError, TypeError, ValueError, OSError, PrerequisiteRefusal):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None


def _response(value, body):
    try:
        if not isinstance(value, dict) or not set(value) <= {'operation_id', 'project_id', 'project_key',
                'delivery_state', 'lead', 'console', 'lead_token', 'console_token'}:
            raise ValueError
        canonical_uuid(value['operation_id']); canonical_uuid(value['project_id'])
        if value['project_key'] != body['project_key'] or value['delivery_state'] not in ('issued_once', 'reissue_required'):
            raise ValueError
        now = datetime.datetime.now(datetime.timezone.utc)
        for role, manager in (('lead', body['lead_key_manager']), ('console', 'kos')):
            record = value[role]; canonical_uuid(record['principal_id'])
            expires = datetime.datetime.fromisoformat(record['expires_at'])
            if record['manager'] != manager or expires.tzinfo is None or expires <= now:
                raise ValueError
        if value['lead']['principal_id'] == value['console']['principal_id']:
            raise ValueError
        tokens = [key in value for key in ('lead_token', 'console_token')]
        if any(tokens) != all(tokens): raise ValueError
        if all(tokens):
            if value['delivery_state'] != 'issued_once': raise ValueError
            for key in ('lead_token', 'console_token'):
                if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value[key]):
                    raise ValueError
        return all(tokens)
    except (ValueError, KeyError, TypeError, PrerequisiteRefusal):
        raise ProvisionRefusal('cortex_provisioning_reissue_required') from None


def _member(value, body, response):
    try:
        expected = {'principal_id': response['console']['principal_id'], 'scope_id': response['project_id'],
                    'project': body['project_key'], 'member_name': 'console', 'actor_kind': 'service',
                    'role': 'member', 'status': 'active', 'project_root': body['repo_root']}
        if not isinstance(value, dict) or any(value[k] != v for k, v in expected.items()): raise ValueError
        canonical_uuid(value['actor_id'])
        if (value['can_read'] is not True or value['can_write'] is not True or value['can_publish'] is not False
                or type(value['roster_revision']) is not int or value['roster_revision'] < 0):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, PrerequisiteRefusal):
        raise ProvisionRefusal('cortex_credential_refused') from None


def run_provision(args: dict, *, owner_token: bytes, ports) -> dict:
    body = validate_request(args)
    if not isinstance(owner_token, bytes) or not re.fullmatch(rb'[A-Za-z0-9_-]{43}', owner_token):
        raise ProvisionRefusal('cortex_provisioning_owner_required')
    try:
        context = ports.validate_installation(args)
        installation = canonical_uuid(context['installation_id'])
        if context['host_os'] != 'linux': raise ProvisionRefusal('cortex_release_unsupported')
        with ports.operation_lock(args) as acquired:
            if acquired is not True: raise ProvisionRefusal('cortex_provisioning_conflict')
            if ports.owner_authorized(owner_token, context) is not True:
                raise ProvisionRefusal('cortex_provisioning_owner_required')
            try:
                response = ports.private_command('create-project', body, owner=owner_token,
                                                  idempotency_key=args['idempotency_key'])
            except TimeoutError:
                raise ProvisionRefusal('cortex_provisioning_reissue_required') from None
            issued = _response(response, body)
            if issued:
                try:
                    ports.store_recipients(response, args)
                except Exception:
                    raise ProvisionRefusal('cortex_provisioning_reissue_required') from None
            if ports.keys_complete(response, args) is not True:
                raise ProvisionRefusal('cortex_provisioning_reissue_required')
            member = _member(ports.member_snapshot(args), body, response)
            proof = ports.native_readiness(args, member)
            if not isinstance(proof, dict) or proof.get('status') != 'READY' or proof.get('installation_id') != installation:
                raise ProvisionRefusal('cortex_health_degraded')
            current = _member(ports.member_snapshot(args), body, response)
            if current != member:
                raise ProvisionRefusal('cortex_provisioning_conflict')
            published = ports.publish_prerequisite(args, member, proof)
            if (not isinstance(published, dict) or set(published) != {'connection_file', 'descriptor_file'}
                    or any(not isinstance(v, str) or not Path(v).is_absolute() for v in published.values())):
                raise ProvisionRefusal('cortex_descriptor_invalid')
            return {'status': 'READY', 'operation_id': response['operation_id'], 'installation_id': installation,
                    'principal_id': member['principal_id'], 'actor_id': member['actor_id'],
                    'scope_id': member['scope_id'], 'connection_file': published['connection_file'],
                    'descriptor_file': published['descriptor_file'], 'expires_at': response['console']['expires_at']}
    except ProvisionRefusal:
        raise
    except PrerequisiteRefusal as error:
        raise ProvisionRefusal(error.code, http_status=error.http_status) from None
    except (OSError, ValueError, TypeError, KeyError):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None
