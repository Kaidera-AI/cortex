"""Prevent project binds from covering API runtime/mount namespaces."""
import ast
import json
import os
from pathlib import Path

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'packages/deploy/cortex-runtime'
AUTHORITY='${CORTEX_PROJECTS_ROOT:?installer must set authoritative projects root}'


def validator():
    node=next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='validated_projects_root')
    ns={'os':os,'Path':Path};exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),ns)
    return ns['validated_projects_root'],node


def protected_paths():
    _,node=validator()
    assignment=next((n for n in node.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='reserved' for t in n.targets)),None)
    assert assignment is not None,'reserved namespace contract absent'
    return tuple(Path(x) for x in ast.literal_eval(assignment.value))


def volume_fields(text):
    fields=[];start=0;depth=0
    for i,ch in enumerate(text):
        if ch=='{':depth+=1
        elif ch=='}':depth-=1
        elif ch==':' and depth==0:fields.append(text[start:i]);start=i+1
    fields.append(text[start:]);return fields


def tripwire(compose,image):
    paths=protected_paths();destinations=[]
    for mount in compose['services']['cortex-api']['volumes']:
        assert isinstance(mount,str),'new mount syntax needs an explicit tripwire update'
        fields=volume_fields(mount);assert len(fields) in {2,3}
        source,destination=fields[:2]
        if source==destination==AUTHORITY:
            assert fields[2:]==['ro'],'intentional projects mapping must remain same-path RO'
            continue # the approved authority itself is not a pre-existing API runtime tree
        assert destination.startswith('/') and '${' not in destination,'unresolved API destination'
        destinations.append(destination)
    destinations+=image['PATH'].split(':')+[image['WorkingDir']]
    for value in destinations:
        path=Path(value);assert path.is_absolute(),value
        assert any(path==p or p in path.parents for p in paths),f'uncovered actual API runtime/mount path: {value}'


@pytest.mark.parametrize('root',['/usr','/usr/local','/app','/tls/app','/tls','/run/cortex-owner','/tmp/projects'])
def test_reserved_equal_contains_or_inside_refused_before_filesystem(monkeypatch,root):
    function,_=validator();monkeypatch.setenv('CORTEX_PROJECTS_ROOT',root)
    with pytest.raises(ValueError,match='reserved'):function()


def test_actual_compose_and_retained_image_paths_all_covered():
    compose=yaml.safe_load((ROOT/'packages/deploy/docker-compose.yml').read_text())
    image=json.loads((ROOT/'packages/deploy/tests/fixtures/api-runtime-paths.json').read_text())
    assert image['config_id']=='sha256:'+image['config_raw_sha256']
    tripwire(compose,image)


def test_tripwire_fails_when_new_api_mount_or_path_is_uncovered():
    compose=yaml.safe_load((ROOT/'packages/deploy/docker-compose.yml').read_text())
    image=json.loads((ROOT/'packages/deploy/tests/fixtures/api-runtime-paths.json').read_text())
    compose['services']['cortex-api']['volumes'].append('new-volume:/future-api-runtime:ro')
    with pytest.raises(AssertionError,match='uncovered'):tripwire(compose,image)
    compose['services']['cortex-api']['volumes'].pop();image['PATH']+=':/future-api-bin'
    with pytest.raises(AssertionError,match='uncovered'):tripwire(compose,image)


def test_default_and_B_fixture_namespaces_pass_exact_overlap_predicate():
    _,node=validator();assignment=next(n for n in node.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='reserved' for t in n.targets));loop=next(n for n in node.body if isinstance(n,ast.For) and n.target.id=='protected')
    code=compile(ast.Module(body=[assignment,loop],type_ignores=[]),str(SOURCE),'exec')
    for root in ['/home/kos/kos','/projects','/home/kos/ren-kos-r426-attempt14-runtime-20261011/fixture-projects']:
        exec(code,{'Path':Path,'path':Path(root)})


def test_valid_existing_isolated_projects_root_passes(tmp_path,monkeypatch):
    root=(tmp_path/'projects');root.mkdir();root=root.resolve();monkeypatch.setenv('CORTEX_PROJECTS_ROOT',str(root));function,_=validator();assert function()==str(root)
