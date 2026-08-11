from sqlalchemy import func, select

from hub.api import audit as audit_mod
from hub.api.errors import ApiError, error_payload
from hub.api.passwords import hash_password, sha256_hex, verify_password
from hub.db.models import AuditEvent
from tests.conftest import make_user


def test_error_payload_shape():
    err = ApiError(422, "HUMAN_APPROVAL_REQUIRED", "缺少人审声明")
    assert error_payload(err) == {
        "error_code": "HUMAN_APPROVAL_REQUIRED",
        "message": "缺少人审声明",
    }


def test_error_payload_with_details():
    err = ApiError(422, "CONTENT_LIMIT_EXCEEDED", "超限",
                   details={"limit": "notes", "actual": 9000, "max": 8192})
    payload = error_payload(err)
    assert payload["details"]["limit"] == "notes"


def test_password_hash_roundtrip():
    h = hash_password("s3cret-pw")
    assert h != "s3cret-pw"
    assert verify_password(h, "s3cret-pw") is True
    assert verify_password(h, "wrong") is False


def test_sha256_hex_deterministic():
    assert sha256_hex("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_record_audit_persists(db_session):
    actor = make_user(db_session, "auditor")
    audit_mod.record_audit(
        db_session, audit_mod.TASK_SUBMITTED,
        actor_user_id=actor.id, matter_id="mat_x", detail={"task_id": "tsk_1"},
    )
    db_session.commit()
    rows = db_session.scalars(select(AuditEvent)).all()
    assert len(rows) == 1
    assert rows[0].event_type == "task_submitted"
    assert rows[0].detail == {"task_id": "tsk_1"}
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 1
