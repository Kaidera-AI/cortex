"""Real lock/recipe mutations through the unchanged complete image driver."""
from pathlib import Path

import pytest

import test_cm2_image_source_payloads as original


def observation(name, tmp_path, monkeypatch):
    state = {'changed': False}
    make_inputs = original.inputs

    def inputs(path):
        root = make_inputs(path)
        control = root / name
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_bytes(b'PUBLIC original build control\n')
        state['control'] = control
        return root

    monkeypatch.setattr(original, 'inputs', inputs)

    class Probe:
        def __getattr__(self, key):
            return getattr(monkeypatch, key)

        def setattr(self, target, key, value, *args, **kwargs):
            if key == 'run' and hasattr(target, 'images'):
                native = value

                def value(argv, **options):
                    result = native(argv, **options)
                    if 'build' in argv and not state['changed']:
                        state['control'].write_bytes(b'PUBLIC changed build control\n')
                        state['changed'] = True
                    return result
            return monkeypatch.setattr(target, key, value, *args, **kwargs)

    def drive():
        original.test_complete_cm2_image_stage_materializes_actual_maps_and_refuses_drift(
            'valid', tmp_path, Probe())

    return state, drive


@pytest.mark.parametrize('name', ['uv.lock', 'deploy/release/Dockerfile.linux-amd64'])
def test_literal_dependency_or_recipe_input_change_refuses_before_success_receipts(name, tmp_path, monkeypatch):
    state, drive = observation(name, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        drive()
    assert state['changed']
    assert state['control'].read_bytes() == b'PUBLIC changed build control\n'
    assert not (tmp_path / 'out/image-inventory.json').exists()
    assert not (tmp_path / 'out/source-payload-inventory.json').exists()
