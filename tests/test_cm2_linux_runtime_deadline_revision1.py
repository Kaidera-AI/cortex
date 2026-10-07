"""R348 ratified clock port: original driver/assertions kept; product unchanged."""
import ast
import inspect
import textwrap
import time

import pytest
from test_cm2_linux_runtime_objects import test_exact_owned_runtime_reads_bind_all_signed_images_and_loopback_network as original_driver
from test_cm2_linux_publication import required


def clock_port(monkeypatch):
    values=[1000.0];sleeps=[]
    monkeypatch.setattr(time,'monotonic',lambda:values[0])
    def sleep(seconds):sleeps.append(seconds);values[0]+=seconds
    monkeypatch.setattr(time,'sleep',sleep)
    return values,sleeps


def test_original_deadline_fixture_preserves_every_assertion_with_exact_clock_port(tmp_path,monkeypatch):
    values,sleeps=clock_port(monkeypatch)
    original_driver('deadline',tmp_path,monkeypatch)
    assert sleeps==[0.08] and values==[1000.08]


def test_original_deadline_fixture_rejects_actual_inverted_product_guard(tmp_path,monkeypatch):
    module=required()
    original=module._read_linux_runtime_uncached
    tree=ast.parse(textwrap.dedent(inspect.getsource(original)))
    mutated=[]
    for node in ast.walk(tree):
        if (isinstance(node,ast.Compare) and isinstance(node.left,ast.Call)
                and isinstance(node.left.func,ast.Attribute) and node.left.func.attr=='monotonic'
                and len(node.ops)==1 and isinstance(node.ops[0],ast.GtE)):
            node.ops[0]=ast.Lt();mutated.append(True)
    assert len(mutated)==1, 'exact actual uncached remaining-deadline predicate required'
    namespace=dict(original.__globals__)
    exec(compile(ast.fix_missing_locations(tree),'<PUBLIC inverted deadline guard>','exec'),namespace)
    monkeypatch.setattr(module,'_read_linux_runtime_uncached',namespace['_read_linux_runtime_uncached'])
    values,sleeps=clock_port(monkeypatch)
    with pytest.raises(AssertionError):original_driver('deadline',tmp_path,monkeypatch)
    assert values==[1000.0] and sleeps==[]
