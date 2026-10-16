"""Legacy completion-acceptance evaluation: unresolved/undecided excluded.

The ``/analytics/studies/{id}/evaluation`` route reports *completion* acceptance
(``had_generation.was_accepted``, a real producer) — distinct from research edit
acceptance, which has no producer. Undecided generations must not be counted as
rejections, and an all-undecided arm reports acceptance as unavailable rather
than zero.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.routers.analytics.studies import evaluate_study

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
STUDY = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _response_body(response):
    return json.loads(response.body)


def _app(acceptance_rate):
    db = MagicMock()

    def execute(clause, params=None):
        sql = str(clause)
        result = MagicMock()
        if "FROM study" in sql:
            result.fetchone.return_value = SimpleNamespace(
                study_id=STUDY, name="s", starts_at=NOW, ends_at=None,
                default_config_id="control",
            )
        else:
            result.fetchall.return_value = [
                SimpleNamespace(
                    assigned_config_id="control", total_users=2, active_users=2,
                    total_queries=5, acceptance_rate=0.5, total_accepted=1,
                    total_generations=2, avg_generation_time=10.0, avg_confidence=0.9,
                    total_sessions=2, avg_serving_time=5.0,
                ),
                SimpleNamespace(
                    assigned_config_id="treatment", total_users=2, active_users=2,
                    total_queries=5, acceptance_rate=acceptance_rate, total_accepted=0,
                    total_generations=2, avg_generation_time=8.0, avg_confidence=0.9,
                    total_sessions=2, avg_serving_time=4.0,
                ),
            ]
        return result

    db.execute.side_effect = execute
    app = MagicMock()
    app.get_db_session.return_value = db
    return app


def test_evaluation_query_excludes_unresolved_generations():
    app = _app(0.5)
    evaluate_study(str(STUDY), current_user=SimpleNamespace(is_admin=True), app=app)

    sql = str(app.get_db_session.return_value.execute.call_args_list[-1].args[0])
    assert "FILTER (WHERE hg.was_accepted IS NOT NULL)" in sql
    assert "ELSE 0.0" not in sql


def test_all_undecided_arm_acceptance_is_unavailable_not_zero():
    app = _app(None)
    response = evaluate_study(
        str(STUDY), current_user=SimpleNamespace(is_admin=True), app=app
    )
    body = _response_body(response)
    treatment = next(
        c for c in body["results"] if c["config_id"] == "treatment"
    )
    # No decided generation -> unavailable (null), never 0%.
    assert treatment["metrics"]["acceptance_rate"] is None
    assert treatment["vs_baseline"]["acceptance_rate_uplift_pct"] is None
    assert treatment["vs_baseline"]["is_better_acceptance"] is None
