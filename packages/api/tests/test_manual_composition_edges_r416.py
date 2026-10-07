"""Additional RED controls from personal interruption and readiness review."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_manual_composition_r416 import runtime,credential,fake_owner,INSTANCE


def test_enrollment_rejects_unsafe_state_before_owner(tmp_path):
    _,obj=runtime(tmp_path); obj.state.chmod(0o755); calls=[]; fake_owner(obj,calls)
    with pytest.raises((ValueError,RuntimeError)): obj.enroll_console()
    assert calls==[]


def test_enrollment_changed_directory_cannot_admit_or_drop_recovery_barrier(tmp_path,monkeypatch):
    _,obj=runtime(tmp_path); calls=[]; fake_owner(obj,calls)
    original=obj.owner_action
    def action(action,**fields):
        result=original(action,**fields)
        if action=='owner-request':
            request=json.loads(Path(fields['request_file']).read_text())
            if request['operation']=='consume-setup':
                Path(obj.args.credential_dir).chmod(0o755)
        return result
    obj.owner_action=action
    with pytest.raises((ValueError,RuntimeError)): obj.enroll_console()
    assert (obj.state/'console-enrollment-pending.json').exists()
    with pytest.raises((ValueError,RuntimeError)): obj.readiness_credential()


def test_boot_without_token_reports_pending_never_healthy(tmp_path):
    _,obj=runtime(tmp_path)
    obj.args.rollback=False
    obj.identity['schema_revision']='a'*64
    obj.acquire_images=obj.prepare=obj.prepare_provider=obj.stop_writers=lambda:None
    obj.compose=lambda *a:None; obj.wait=lambda *a,**k:None
    obj.check=lambda:pytest.fail('fresh unauthenticated startup must not report readiness')
    result=obj.up()
    assert result=={'started':True,'readiness':'console-enrollment-required','healthy':False}


def test_enrollment_and_unmodified_signed_writer_agree_on_all_credential_fields(tmp_path,monkeypatch):
    from test_manual_prerequisite_r407 import required,seed
    writer=required(monkeypatch)
    args,manifest,health,expected,calls,_=seed(tmp_path)
    _,obj=runtime(tmp_path)
    obj.args.credential_dir=str(tmp_path/'enrolled')
    obj.args.credential_project='notes'
    owner_calls=[]; fake_owner(obj,owner_calls)
    receipt=obj.enroll_console()
    args['credential_dir']=Path(receipt['credential_dir'])
    writer.write_prerequisite(**args)
    descriptor=json.loads((args['home']/'.cortex/prerequisite.json').read_text())
    assert {field:descriptor[field] for field in ('credential_dir','credential_file','credential_project','credential_agent')} == {
        field:receipt[field] for field in ('credential_dir','credential_file','credential_project','credential_agent')}
    assert calls==['http://127.0.0.1:8501/health']
