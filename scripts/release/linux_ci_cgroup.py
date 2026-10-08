"""Read-only CI cgroup capability checks; never removes product limits."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install_candidate import ROLES, container_args

ROOT = Path(__file__).resolve().parents[2]
FLAGS = {'--cpus':'cpu', '--cpu-quota':'cpu', '--cpu-period':'cpu', '--cpu-shares':'cpu',
         '--cpuset-cpus':'cpuset', '--cpuset-mems':'cpuset', '--blkio-weight':'io',
         '--device-read-bps':'io', '--device-write-bps':'io', '--device-read-iops':'io',
         '--device-write-iops':'io', '--memory':'memory', '--pids-limit':'pids'}
COMPOSE = {'cpus':'cpu','cpu_quota':'cpu','cpu_period':'cpu','cpu_shares':'cpu','cpuset':'cpuset',
           'blkio_config':'io','mem_limit':'memory','pids_limit':'pids'}


def _read(path):
    path = Path(path)
    if path.resolve() != path.absolute() or not path.is_file(): raise ValueError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream: raw = stream.read(8193)
    if len(raw) > 8192: raise ValueError
    return raw.decode('ascii')


def _path(value):
    if (not isinstance(value, str) or not value.startswith('/') or '..' in PurePosixPath(value).parts
            or re.fullmatch(r'/[a-zA-Z0-9_./@\\-]*', value) is None
            or str(PurePosixPath(value)) != value): raise ValueError
    return value


def _controllers(path):
    text = _read(path)
    if re.fullmatch(r'[a-z0-9_\s]*', text) is None: raise ValueError
    return sorted(set(text.split()))


def _node(root, path):
    path = _path(path); node = root / path.lstrip('/')
    if node.resolve() != node.absolute(): raise ValueError
    parent = node.parent if node != root else root
    return {'path':path, 'controllers':_controllers(node/'cgroup.controllers'),
            'parent_subtree_control':_controllers(parent/'cgroup.subtree_control')}


def specification(source_root):
    run_services = []
    record = {'installation':'10000000-0000-4000-8000-000000000001'}
    required = set()
    for role in (*ROLES, 'migrate'):
        flags = [arg for arg in container_args(role,record) if arg in FLAGS]
        required.update(FLAGS[arg] for arg in flags)
        run_services.append({'service':role,'flags':flags})
    compose_services = []
    hashes = {}
    run_file = source_root/'scripts/release/install_candidate.py'
    hashes[str(run_file.relative_to(source_root))] = hashlib.sha256(run_file.read_bytes()).hexdigest()
    for path in sorted((source_root/'deploy').glob('compose*.yaml')):
        hashes[str(path.relative_to(source_root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        service = None
        for line in path.read_text().splitlines():
            match = re.fullmatch(r'  ([a-zA-Z0-9_-]+):\s*',line)
            if match: service = match.group(1)
            field = re.match(r'    ([a-z_]+):',line)
            if service and field and field.group(1) in COMPOSE:
                compose_services.append({'file':str(path.relative_to(source_root)), 'service':service,
                                         'field':field.group(1),'controller':COMPOSE[field.group(1)]})
    return {'run_services':run_services,'compose_services':compose_services,'source_hashes':hashes}, sorted(required)


def observe(*, source_root=ROOT, cgroup_root=Path('/sys/fs/cgroup'),
            proc_cgroup=None, manager_path=None):
    value = {'schema':'cortex.cgroup-capability.v1','status':'FAIL','category':'cgroup_observation_unavailable'}
    try:
        spec, required = specification(Path(source_root));value.update(specification=spec,required=required)
        # /proc/self is a kernel symlink; use the same process's physical PID path.
        lines = _read(Path(f'/proc/{os.getpid()}/cgroup') if proc_cgroup is None else proc_cgroup).splitlines()
        if len(lines)!=1 or not lines[0].startswith('0::'): raise ValueError
        current_path = _path(lines[0][3:])
        if manager_path is None:
            result = subprocess.run(['systemctl','show',f'user@{os.getuid()}.service','--property=ControlGroup','--value'],
                                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,check=True)
            if len(result.stdout)>4096: raise ValueError
            manager_path = result.stdout.decode('ascii').strip()
        root = Path(cgroup_root)
        if root.resolve()!=root.absolute(): raise ValueError
        current = _node(root,current_path); manager = _node(root,manager_path)
        value.update(current=current,manager=manager)
        for controller in required:
            if any(controller not in node['controllers'] or controller not in node['parent_subtree_control']
                   for node in (current,manager)):
                value['category']='cgroup_controller_not_delegated:'+controller
                return value
        value.update(status='PASS',category='none')
    except Exception:
        pass
    return value


def require(receipt):
    if receipt.get('status')!='PASS':
        category = receipt.get('category','cgroup_observation_unavailable')
        if re.fullmatch(r'cgroup_controller_not_delegated:(?:cpu|cpuset|io|memory|pids)',category) is None:
            category='cgroup_observation_unavailable'
        raise RuntimeError(category)


def write_receipt(destination, value):
    destination = Path(destination)
    if destination.parent.resolve()!=destination.parent.absolute(): raise RuntimeError('cgroup_receipt_custody')
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())


def check(destination):
    value=observe();write_receipt(destination,value);require(value);return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--receipt',required=True);parser.add_argument('--expect-missing',choices=['cpu']);args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.getuid()==0: raise RuntimeError('owned_ci_cgroup_required')
    value=observe();write_receipt(args.receipt,value)
    if args.expect_missing:
        if value['category']!='cgroup_controller_not_delegated:'+args.expect_missing: raise RuntimeError('expected_cgroup_red_not_observed')
        print(value['category']);return
    require(value)

if __name__=='__main__': main()
