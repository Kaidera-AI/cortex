#!/usr/bin/env python3
"""Contained CLI fault fixture; never calls Podman, Git, PG or application tests."""
import hashlib
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ['REPLAY_ENGINE_STATE'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
kind = None
exit_code = 0
stdout = ''
state['commands'].append(args)
if Path(sys.argv[0]).name == 'git':
    stdout = 'fixture-head\n'
elif args[:2] == ['image', 'inspect']:
    stdout = '[{"Architecture":"arm64"}]\n'
elif args[:2] == ['pod', 'create'] or args[0] == 'run':
    kind = 'pod' if args[0] == 'pod' else 'container'
    name = args[args.index('--name') + 1]
    state['output_present_before_create'] = Path(os.environ['REPLAY_FAKE_OUT']).is_dir()
    labels = {}
    for i, arg in enumerate(args):
        if arg == '--label':
            key, value = args[i + 1].split('=', 1)
            labels[key] = value
    if not state['resources']:
        state['resources'][name] = {
            'kind': kind, 'name': name, 'id': hashlib.sha256((kind + name).encode()).hexdigest(),
            'labels': labels}
    # Realistic external effect BEFORE failed/ambiguous acknowledgement.
    exit_code = 1
elif len(args) > 1 and args[1] == 'exists':
    exit_code = 0 if args[-1] in state['resources'] else 1
elif args[:2] in (['pod', 'inspect'], ['container', 'inspect']):
    row = state['resources'][args[-1]]
    if args[0] == 'pod':
        stdout = json.dumps([{'Id': row['id'], 'Name': row['name'], 'Labels': row['labels']}])
    else:
        stdout = json.dumps([{'Id': row['id'], 'Name': '/' + row['name'], 'Config': {'Labels': row['labels']}}])
elif args[:3] == ['pod', 'rm', '-f'] or args[:2] == ['rm', '-f']:
    target = args[-1]
    for name, row in list(state['resources'].items()):
        if target in (name, row['id']):
            state['removed'].append({'target': target, **row})
            del state['resources'][name]
            break
    else:
        exit_code = 1
elif args[0] in ('logs', 'exec'):
    exit_code = 1  # No PG/driver/application exists; never synthesize successful tests.
else:
    exit_code = 125
state_path.write_text(json.dumps(state))
print(stdout, end='')
if exit_code:
    print('declared contained CLI fault; no application executed', file=sys.stderr)
raise SystemExit(exit_code)
