"""An unchanged namespace never authorizes work after the shared deadline."""
import pytest

from test_cm2_linux_review_repairs import actual_runtime


@pytest.mark.parametrize('case', ['inventory', 'local-context', 'copy'])
def test_runtime_read_refuses_when_final_custody_work_crosses_deadline(case, tmp_path, monkeypatch):
    host, custody, root, *_ = actual_runtime(tmp_path, monkeypatch)
    policy = {'minimum_version': '6.0.2'}; clock = [0.0]
    monkeypatch.setattr(host.time, 'monotonic', lambda: clock[0])
    with host.runtime_observation_scope(root, kos_policy=policy, deadline=2):
        with monkeypatch.context() as change:
            if case == 'inventory':
                original = host._RuntimeObservation.package_inventory
                def inventory(self):
                    value = original(self); clock[0] = 3.0; return value
                change.setattr(host._RuntimeObservation, 'package_inventory', inventory)
            elif case == 'local-context':
                original = host._local_engine_context
                def context():
                    value = original(); clock[0] = 3.0; return value
                change.setattr(host, '_local_engine_context', context)
            else:
                original = host.copy.deepcopy
                def copying(value):
                    result = original(value); clock[0] = 3.0; return result
                change.setattr(host.copy, 'deepcopy', copying)
            error = None
            try:
                host.read_linux_runtime(root, kos_policy=policy, deadline=2)
            except custody.PrerequisiteRefusal as refusal:
                error = refusal
            finally:
                clock[0] = 0.0
    assert error is not None and error.code == 'cortex_health_unavailable'
