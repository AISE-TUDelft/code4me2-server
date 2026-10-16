"""A window's never-started session ends once silent past the idle timeout.

The window would otherwise keep it across restarts days apart, and its interval
would run from the first start to the latest heartbeat (pure session service).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from research.analysis.study_analytics import metrics
from research.analysis.study_analytics.models import SessionRow
from research.runtime.sessions.enums import CloseReason, SessionState
from research.runtime.sessions.models import ResearchSessionV1, SessionPolicyV1
from research.runtime.sessions.service import expire_if_idle

MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
POLICY = SessionPolicyV1(idle_timeout_seconds=600, resume_grace_seconds=120, heartbeat_seconds=30)


def _session(state: SessionState, *, heartbeat: datetime, activity: datetime | None = None) -> ResearchSessionV1:
    return ResearchSessionV1(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        context_id="ctx-1",
        state=state,
        opened_at=MONDAY,
        last_heartbeat_at=heartbeat,
        last_activity_at=activity,
    )


def test_a_never_started_window_session_ends_when_the_window_comes_back_days_later():
    # The IDE was open (heartbeats) until 17:00 on Monday, the participant only
    # chatted, and the window starts again on Wednesday.
    last_heartbeat = MONDAY + timedelta(hours=8)
    wednesday = MONDAY + timedelta(days=2, hours=5)

    result = expire_if_idle(_session(SessionState.NOT_STARTED, heartbeat=last_heartbeat), wednesday, policy=POLICY)

    assert result.accepted and result.transition is not None
    assert result.session.state == SessionState.ENDED
    assert result.session.close_reason == CloseReason.IDLE_TIMEOUT
    # Analytics count Monday's window, not the absence.
    row = SessionRow(
        session_id="s1",
        enrollment_id="e1",
        state="ended",
        opened_at=result.session.opened_at,
        closed_at=result.session.closed_at,
        last_activity_at=result.session.last_activity_at,
        last_heartbeat_at=result.session.last_heartbeat_at,
        close_reason=result.session.close_reason.value,
    )
    assert metrics.session_seconds(row) == 8 * 3600


def test_a_never_started_session_with_a_live_window_stays_open():
    now = MONDAY + timedelta(hours=3)
    result = expire_if_idle(
        _session(SessionState.NOT_STARTED, heartbeat=now - timedelta(seconds=30)), now, policy=POLICY
    )
    assert result.transition is None and result.session.state == SessionState.NOT_STARTED


def test_a_running_session_still_expires_on_its_last_activity_not_its_heartbeat():
    now = MONDAY + timedelta(hours=3)
    idle = _session(SessionState.RUNNING, heartbeat=now - timedelta(seconds=30), activity=now - timedelta(hours=1))
    assert expire_if_idle(idle, now, policy=POLICY).session.state == SessionState.ENDED
    busy = _session(SessionState.RUNNING, heartbeat=now - timedelta(hours=1), activity=now - timedelta(minutes=5))
    assert expire_if_idle(busy, now, policy=POLICY).session.state == SessionState.RUNNING


def test_a_placeholder_that_waited_past_the_timeout_starts_at_the_first_activity():
    # 18:00 the participant stops; 18:10 the idle rotation creates a placeholder
    # that heartbeats keep alive overnight (IDE open, machine awake); 09:10 the
    # first edit. The night is not session time.
    from research.runtime.sessions.service import on_qualifying_activity

    rotated_at = MONDAY + timedelta(hours=9, minutes=10)
    morning = rotated_at + timedelta(hours=15)
    placeholder = _session(SessionState.NOT_STARTED, heartbeat=morning - timedelta(seconds=30)).model_copy(
        update={"opened_at": rotated_at}
    )

    started = on_qualifying_activity(placeholder, morning, idle_timeout_seconds=POLICY.idle_timeout_seconds)

    assert started.session.state == SessionState.RUNNING
    assert started.session.opened_at == morning
    # A window used a few minutes after it opened keeps its own start.
    soon = rotated_at + timedelta(minutes=5)
    fresh = placeholder.model_copy(update={"last_heartbeat_at": soon})
    assert on_qualifying_activity(fresh, soon, idle_timeout_seconds=600).session.opened_at == rotated_at
    # Without a policy (other callers) nothing changes.
    assert on_qualifying_activity(placeholder, morning).session.opened_at == rotated_at


def test_a_study_cannot_set_an_idle_timeout_within_one_heartbeat():
    import pytest
    from fastapi import HTTPException

    from backend.routers.research.studies import _validated_session_policy

    with pytest.raises(HTTPException) as error:
        _validated_session_policy({"idle_timeout_seconds": 30, "resume_grace_seconds": 60, "heartbeat_seconds": 30})
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SESSION_POLICY_INVALID"
    # The form's presets stay valid.
    for policy in (
        {"idle_timeout_seconds": 600, "resume_grace_seconds": 120, "heartbeat_seconds": 30},
        {"idle_timeout_seconds": 3600, "resume_grace_seconds": 900, "heartbeat_seconds": 60},
        {"idle_timeout_seconds": 600, "resume_grace_seconds": 120},
    ):
        assert _validated_session_policy(policy)["idle_timeout_seconds"] == policy["idle_timeout_seconds"]
