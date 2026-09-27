"""Bootstrap must not reuse a context session past its idle deadline."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from backend.routers.research.bootstrap import _PersistentSessionFactory
from research.runtime.sessions.enums import CloseReason, SessionState
from research.runtime.sessions.models import ResearchSessionV1


def test_bootstrap_retires_idle_context_before_issuing_new_session(monkeypatch):
    from backend.routers.research import bootstrap

    now = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)
    enrollment = SimpleNamespace(enrollment_id=uuid4())
    study = SimpleNamespace(
        study_id=uuid4(),
        research_config_json={"session_policy": {
            "idle_timeout_seconds": 900,
            "resume_grace_seconds": 300,
            "heartbeat_seconds": 30,
        }},
    )
    previous = ResearchSessionV1(
        research_session_id=uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_id=study.study_id,
        context_id="window-a",
        state=SessionState.RUNNING,
        opened_at=now - timedelta(minutes=30),
        last_activity_at=now - timedelta(minutes=20),
    )
    old_row = SimpleNamespace(session_id=previous.research_session_id,
                              opened_at=previous.opened_at)
    saved = []
    monkeypatch.setattr(bootstrap.session_store, "get_active_session_for_context",
                        lambda *_: old_row)
    monkeypatch.setattr(bootstrap.session_store, "row_to_session", lambda *_: previous)
    monkeypatch.setattr(bootstrap.session_store, "insert_transition",
                        lambda *args, **kwargs: saved.append(("transition", args[1], kwargs)))
    monkeypatch.setattr(bootstrap.session_store, "update_session",
                        lambda *args, **kwargs: saved.append(("session", args[1], kwargs)))
    monkeypatch.setattr(bootstrap.session_store, "create_session",
                        lambda _db, session, **_kw: SimpleNamespace(
                            session_id=session.research_session_id, opened_at=session.opened_at))

    result = _PersistentSessionFactory(object(), commit=False).create_for_enrollment(
        enrollment, study, now, "window-a"
    )

    assert result.research_session_id != previous.research_session_id
    assert saved[0][0] == "transition"
    assert saved[0][2] == {"commit": False}
    assert saved[1][1].state is SessionState.ENDED
    assert saved[1][1].close_reason is CloseReason.IDLE_TIMEOUT
    assert saved[1][2] == {"commit": False}


def test_bootstrap_reuses_context_before_idle_deadline(monkeypatch):
    from backend.routers.research import bootstrap

    now = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)
    enrollment = SimpleNamespace(enrollment_id=uuid4())
    study = SimpleNamespace(
        study_id=uuid4(),
        research_config_json={"session_policy": {
            "idle_timeout_seconds": 900,
            "resume_grace_seconds": 300,
        }},
    )
    previous = ResearchSessionV1(
        research_session_id=uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_id=study.study_id,
        context_id="window-a",
        state=SessionState.RUNNING,
        opened_at=now - timedelta(minutes=4),
        last_activity_at=now - timedelta(minutes=1),
    )
    old_row = SimpleNamespace(session_id=previous.research_session_id,
                              opened_at=previous.opened_at)
    monkeypatch.setattr(bootstrap.session_store, "get_active_session_for_context",
                        lambda *_: old_row)
    monkeypatch.setattr(bootstrap.session_store, "row_to_session", lambda *_: previous)
    monkeypatch.setattr(bootstrap.session_store, "create_session",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("new session")))

    result = _PersistentSessionFactory(object()).create_for_enrollment(
        enrollment, study, now, "window-a"
    )

    assert result.research_session_id == previous.research_session_id
