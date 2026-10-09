"""Read-only guard: require Cortex repository, branch and PR identity before edits."""
import json
import subprocess
import sys

repo, number, branch = sys.argv[1:]
value = json.loads(subprocess.check_output([
    'gh', 'pr', 'view', number, '-R', repo, '--json', 'url,headRefName,headRefOid,baseRefName'], text=True))
assert value['url'] == 'https://github.com/Kaidera-AI/cortex/pull/' + number, 'wrong repository target'
assert value['headRefName'] == branch, 'wrong source branch target'
print(json.dumps({'target_guard': 'PASS', **value}))
