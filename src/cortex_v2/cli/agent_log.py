"""Native implementation of the frozen cortex-log command; no shell credential."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlencode
import uuid

from ..clients.config import _validate_base_url
from ..clients.errors import ClientConfigError
from ..clients.transport import http_request

TYPES = ('commit', 'decision', 'lesson', 'started', 'stopped', 'blocked', 'unblocked', 'bug', 'handoff', 'question')


def _refuse():
    return ClientConfigError('log request or confirmation refused')


def _id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise _refuse()
    return value


def prepare(args):
    supplied = args.agent.lower()
    name = supplied.split('@', 1)[0].split(':', 1)[0]
    if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,95}', name) or args.event_type not in TYPES
            or not args.summary or '\0' in args.summary or len(args.files) > 1024
            or any('\0' in f for f in args.files)):
        raise _refuse()
    goal = args.goal if args.goal is not None else os.environ.get('CORTEX_PARENT_GOAL_ID', os.environ.get('CORTEX_EPIC_ID', ''))
    summary = args.summary
    if goal and not summary.startswith('[GOAL:'):
        summary = '[GOAL:' + goal + '] ' + summary
    if len(summary) > 65536 or '\0' in goal:
        raise _refuse()
    payload = {'event_type': args.event_type, 'summary': summary}
    if args.files:
        payload['files_affected'] = args.files
    if goal:
        payload['metadata'] = {'parent_goal_id': goal, 'goal_ancestry': [goal]}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 1024**2:
        raise _refuse()
    return name, payload


def run(profile, args, prepared):
    name, payload = prepared
    reader = profile.member_reader
    if (reader is None or profile.default_scope != reader.project
            or any(s != reader.project for s in profile.default_read_scopes)
            or name != profile.principal_label):
        raise _refuse()
    origin = _validate_base_url(profile.base_url)

    def request(method, path, body=None, key=None):
        headers = dict(reader.headers())
        if (headers.get('X-Cortex-Scope') != reader.project
                or set(headers) != {'Authorization', 'X-Cortex-Scope'}
                or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', headers.get('Authorization', ''))):
            raise _refuse()
        headers['X-Agent-Name'] = name
        if key:
            headers['Idempotency-Key'] = key
        response = http_request(method, origin + path, headers=headers, json_body=body)
        if response.status != 200:
            raise _refuse()
        from ..clients.native_prerequisite import strict_json
        try:
            return strict_json(response.body, limit=1024**2)
        except Exception:
            raise _refuse() from None

    response = request('POST', '/log', payload, str(uuid.uuid4()))
    if response.get('verified') is not True or not (response.get('logged') is True or response.get('id')):
        raise _refuse()
    own = _id(response.get('id'))
    companion = response.get('team_event_id')
    kind = args.event_type if args.event_type in ('decision', 'lesson') else 'team_event'
    if kind != 'team_event':
        companion = _id(companion)
    elif companion is not None:
        raise _refuse()
    confirm = args.confirm if args.confirm is not None else os.environ.get('CORTEX_WRITE_CONFIRM', '1') == '1'
    if confirm:
        checks = [(kind, own)] + ([('team_event', companion)] if companion else [])
        for selected_kind, row_id in checks:
            data = request('GET', '/verify/write?' + urlencode({'kind': selected_kind, 'id': row_id}))
            row = data.get('row', {})
            if (data.get('verified') is not True or data.get('kind') != selected_kind or data.get('id') != row_id
                    or row.get('summary') != payload['summary']):
                raise _refuse()
            if selected_kind == 'team_event' and (row.get('event_type') != args.event_type
                    or row.get('files') != payload.get('files_affected', [])):
                raise _refuse()
    return '\033[32mLogged: [' + args.event_type + '] ' + name + ' — ' + payload['summary'] + '\033[0m\n'
