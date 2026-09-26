"""HTTP contract of the participant-budget API (owner/admin), prices and surfaces."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from backend.routers.analytics.auth_utils import AuthenticatedUser
from research.budget import ledger
from research.budget.models import UsageSnapshot
from research.budget.pricing import get_model_price

from ._ui_overhaul_seed import (
    admin_user,
    participant_user,
    researcher_user,
    seed_account,
    seed_assignment,
    seed_byoa_release,
    seed_connection,
    seed_enrollment,
    seed_packaged_release,
    seed_participant,
    seed_profile,
    seed_study,
)
from .test_research_api_contract import VALID_SESSION_POLICY, http_runtime  # noqa: F401 - fixture

MODEL = "goose-model"


def _price(db, connection_id, model=MODEL, input_price="1.0", output_price="4.0"):
    db.execute(
        text(
            "INSERT INTO public.provider_model_price (connection_id, model, input_usd_per_million, "
            "output_usd_per_million, updated_at) VALUES (:c, :m, :i, :o, now()) "
            "ON CONFLICT (connection_id, model) DO UPDATE SET input_usd_per_million = EXCLUDED.input_usd_per_million"
        ),
        {"c": connection_id, "m": model, "i": input_price, "o": output_price},
    )
    db.commit()


class Seeded:
    def __init__(self, session_factory, monkeypatch, *, framework="goose", priced=True, default_micro_usd=10_000_000):
        self.factory = session_factory
        monkeypatch.setenv("UI_OVERHAUL_KEY", "k")
        with session_factory() as db:
            self.owner_id = seed_account(db, "owner@example.com", can_research=True)
            self.other_researcher_id = seed_account(db, "other@example.com", can_research=True)
            self.admin_id = seed_account(db, "admin@example.com", is_admin=True, can_research=True)
            self.account_id = seed_account(db, "participant@example.com")
            self.connection_id = seed_connection(db, models=(MODEL,), priced=False)
            if priced:
                _price(db, self.connection_id)
            release_id = (
                seed_byoa_release(db, agent_id=framework, agent_package=framework, agent_command=framework)
                if framework != "code4me2-agent"
                else seed_packaged_release(db)
            )
            self.profile_id = seed_profile(
                db, owner_id=self.owner_id, framework_version=framework, release_id=release_id,
                connection_id=self.connection_id, name="arm", model=MODEL,
            )
            self.study_id = seed_study(db, owner_id=self.owner_id, name="budget study")
            db.execute(
                text(
                    "INSERT INTO public.study_agent_profile (study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                    "VALUES (:s, :p, 'digest', CAST(:snap AS jsonb), 0, now())"
                ),
                {
                    "s": self.study_id, "p": self.profile_id,
                    "snap": __import__("json").dumps({
                        "profile_id": str(self.profile_id), "name": "arm", "model": MODEL,
                        "framework_version": framework, "connection_id": str(self.connection_id),
                        "tools_json": "[]", "approval_policy": "auto", "max_steps": 3, "release_id": release_id,
                    }),
                },
            )
            db.execute(
                text("UPDATE public.study SET inference_budget_default_micro_usd = :d WHERE study_id = :s"),
                {"d": default_micro_usd, "s": self.study_id},
            )
            db.commit()
            self.participant_id = seed_participant(db, self.account_id)
            self.enrollment_id = seed_enrollment(db, participant_id=self.participant_id, study_id=self.study_id)
            seed_assignment(
                db, enrollment_id=self.enrollment_id, study_id=self.study_id, profile_id=self.profile_id,
                snapshot={"profile_id": str(self.profile_id), "name": "arm", "model": MODEL, "framework_version": framework,
                          "connection_id": str(self.connection_id), "tools_json": "[]", "approval_policy": "auto", "max_steps": 3},
            )
            ledger.create_balance(db, enrollment_id=self.enrollment_id, study_id=self.study_id, limit_micro_usd=default_micro_usd, commit=True)
            self.price = get_model_price(db, self.connection_id, MODEL) if priced else None

    def spend(self, prompt_tokens=100, completion_tokens=50):
        with self.factory() as db:
            reservation = ledger.reserve(
                db, enrollment_id=self.enrollment_id, study_id=self.study_id, connection_id=self.connection_id,
                model=MODEL, entry_point="research_gateway", request_id=str(uuid.uuid4()),
                openai_body={"messages": [{"role": "user", "content": "x" * 100}], "max_tokens": 256},
                upstream_base_url="https://provider.test/v1", price=self.price,
                settings=__import__("research.budget.settings", fromlist=["BudgetSettings"]).BudgetSettings(),
            )
            ledger.settle(db, reservation.reservation_id, usage=UsageSnapshot(prompt_tokens, completion_tokens, finish_reason="stop"), price=self.price)
        return prompt_tokens * 1 + completion_tokens * 4


BUDGET = "/api/research/studies/{study}/budget"
ENROLLMENT_BUDGET = "/api/research/studies/{study}/enrollments/{enrollment}/budget"


def test_study_budget_get_patch_and_authorization(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch)
    url = BUDGET.format(study=seeded.study_id)

    current_user["value"] = researcher_user(seeded.owner_id)
    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metered"] is True
    assert body["metered_profile_ids"] == [str(seeded.profile_id)]
    assert body["default_budget_micro_usd"] == 10_000_000 and body["default_budget_usd"] == "10.00"
    assert body["warning_fraction"] == 0.8 and body["editable"] is True
    assert body["participants"] == {"total": 1, "on_default": 1, "on_old_default": 0, "custom": 0, "exhausted": 0}
    assert body["pricing"] == {"complete": True, "missing": []}
    assert body["metered_spend_micro_usd"] == 0

    updated = client.patch(url, json={"default_budget_usd": "12.50", "warning_fraction": 0.9})
    assert updated.status_code == 200, updated.text
    assert updated.json()["default_budget_micro_usd"] == 12_500_000
    assert updated.json()["warning_fraction"] == 0.9
    assert updated.json()["updated_by"] == researcher_user(seeded.owner_id).email
    assert updated.json()["participants"]["on_old_default"] == 1

    for bad in ({"default_budget_usd": "-1"}, {"default_budget_usd": "abc"}, {"default_budget_usd": "1.1234567"}, {}):
        assert client.patch(url, json=bad).status_code == 422, bad

    # Other researcher and participant: 403; admin: 200.
    current_user["value"] = researcher_user(seeded.other_researcher_id)
    assert client.get(url).status_code == 403
    assert client.patch(url, json={"default_budget_usd": "1"}).status_code == 403
    current_user["value"] = participant_user(seeded.account_id)
    assert client.get(url).status_code == 403
    current_user["value"] = admin_user(seeded.admin_id)
    assert client.get(url).status_code == 200
    assert client.get(BUDGET.format(study=uuid.uuid4())).status_code == 404


def test_apply_default_touches_only_rows_still_on_the_default(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch)
    current_user["value"] = researcher_user(seeded.owner_id)
    url = BUDGET.format(study=seeded.study_id)
    # Second participant with a manual limit.
    with session_factory() as db:
        other_account = seed_account(db, "p2@example.com")
        p2 = seed_participant(db, other_account)
        e2 = seed_enrollment(db, participant_id=p2, study_id=seeded.study_id)
        ledger.create_balance(db, enrollment_id=e2, study_id=seeded.study_id, limit_micro_usd=10_000_000, commit=True)
        ledger.adjust(db, enrollment_id=e2, kind="SET_LIMIT", amount_micro_usd=3_000_000, actor="a", reason="manual")
    assert client.patch(url, json={"default_budget_usd": "20"}).status_code == 200
    applied = client.post(url + "/apply-default", json={"reason": "raise", "idempotency_key": "apply-0001"})
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"applied": 1, "skipped": 1, "default_budget_micro_usd": 20_000_000, "default_budget_usd": "20.00"}
    replay = client.post(url + "/apply-default", json={"reason": "raise", "idempotency_key": "apply-0001"})
    assert replay.status_code == 200 and replay.json()["applied"] == 0
    with session_factory() as db:
        assert ledger.balance_view(db, seeded.enrollment_id).limit_micro_usd == 20_000_000
        assert ledger.balance_view(db, e2).limit_micro_usd == 3_000_000
    assert client.post(url + "/apply-default", json={"reason": "", "idempotency_key": "apply-0002"}).status_code == 422
    assert client.post(url + "/apply-default", json={"reason": "x", "idempotency_key": "short"}).status_code == 422


def test_enrollment_budget_adjustments_ledger_and_pagination(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch)
    current_user["value"] = researcher_user(seeded.owner_id)
    url = ENROLLMENT_BUDGET.format(study=seeded.study_id, enrollment=seeded.enrollment_id)
    charged = seeded.spend()

    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metered"] is True
    assert body["balance"]["limit_micro_usd"] == 10_000_000
    assert body["balance"]["consumed_micro_usd"] == charged
    assert body["balance"]["reserved_micro_usd"] == 0
    assert body["balance"]["remaining_micro_usd"] == 10_000_000 - charged
    assert body["balance"]["limit_usd"] == "10.00"
    assert body["ledger_summary"]["calls"] == 1 and body["ledger_summary"]["settled_micro_usd"] == charged
    assert body["recent_adjustments"] == []

    top_up = client.post(url + "/adjustments", json={"kind": "TOP_UP", "amount_usd": "5.00", "reason": "more", "idempotency_key": "topup-0001"})
    assert top_up.status_code == 201, top_up.text
    assert top_up.json()["replayed"] is False
    assert top_up.json()["adjustment"]["delta_micro_usd"] == 5_000_000
    assert top_up.json()["balance"]["limit_micro_usd"] == 15_000_000
    replay = client.post(url + "/adjustments", json={"kind": "TOP_UP", "amount_usd": "5.00", "reason": "more", "idempotency_key": "topup-0001"})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["balance"]["limit_micro_usd"] == 15_000_000
    conflict = client.post(url + "/adjustments", json={"kind": "TOP_UP", "amount_usd": "9.00", "reason": "more", "idempotency_key": "topup-0001"})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    lowered = client.post(url + "/adjustments", json={"kind": "SET_LIMIT", "amount_usd": "0", "reason": "stop", "idempotency_key": "set-0001"})
    assert lowered.status_code == 201
    assert lowered.json()["balance"]["exhausted"] is True
    for bad in (
        {"kind": "TOP_UP", "amount_usd": "0", "reason": "r", "idempotency_key": "bad-00001"},
        {"kind": "REFUND", "amount_usd": "1", "reason": "r", "idempotency_key": "bad-00002"},
        {"kind": "TOP_UP", "amount_usd": "1", "reason": "", "idempotency_key": "bad-00003"},
        {"kind": "TOP_UP", "amount_usd": "x", "reason": "r", "idempotency_key": "bad-00004"},
    ):
        assert client.post(url + "/adjustments", json=bad).status_code == 422, bad

    ledger_page = client.get(url + "/ledger", params={"limit": 1})
    assert ledger_page.status_code == 200
    assert len(ledger_page.json()["entries"]) == 1 and ledger_page.json()["entries"][0]["state"] == "SETTLED"
    assert ledger_page.json()["next_cursor"] is not None
    second = client.get(url + "/ledger", params={"limit": 1, "cursor": ledger_page.json()["next_cursor"]})
    assert second.status_code == 200 and second.json()["entries"] == []
    assert client.get(url + "/ledger", params={"cursor": "not-a-date"}).status_code == 422
    history = client.get(url + "/adjustments")
    assert [item["kind"] for item in history.json()["adjustments"]] == ["SET_LIMIT", "TOP_UP"]

    # Foreign enrollment / other researcher.
    assert client.get(ENROLLMENT_BUDGET.format(study=seeded.study_id, enrollment=uuid.uuid4())).status_code == 404
    current_user["value"] = researcher_user(seeded.other_researcher_id)
    assert client.get(url).status_code == 403
    assert client.post(url + "/adjustments", json={"kind": "TOP_UP", "amount_usd": "1", "reason": "r", "idempotency_key": "other-0001"}).status_code == 403


def test_participants_table_summary_and_my_studies_carry_budgets(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch)
    charged = seeded.spend()
    current_user["value"] = researcher_user(seeded.owner_id)
    table = client.get(f"/api/research/studies/{seeded.study_id}/analytics/participants")
    assert table.status_code == 200, table.text
    row = next(item for item in table.json()["participants"] if item["enrollment_id"] == str(seeded.enrollment_id))
    assert row["budget"]["limit_micro_usd"] == 10_000_000
    assert row["budget"]["consumed_micro_usd"] == charged and row["budget"]["exhausted"] is False
    summary = client.get(f"/api/research/studies/{seeded.study_id}/analytics/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["totals"]["metered_spend_micro_usd"] == charged
    assert summary.json()["totals"]["metered_calls"] == 1

    current_user["value"] = participant_user(seeded.account_id)
    me = client.get("/api/research/participants/me")
    assert me.status_code == 200
    mine = me.json()["enrollments"][0]
    assert mine["runtime"]["credentials"] == "shared"
    budget = mine["budget"]
    assert budget["limit"] == 10_000_000 and budget["consumed"] == charged
    assert budget["remaining"] == 10_000_000 - charged and budget["exhausted"] is False
    assert set(budget) == {"unit", "limit", "consumed", "reserved", "remaining", "fraction_used", "warning_fraction", "warning", "exhausted", "exhausted_at", "as_of"}
    # Arm-blind: no model, profile or price anywhere in the participant view.
    assert MODEL not in me.text and "arm" not in str(mine["budget"])


def test_study_create_requires_budget_and_prices_for_metered_arms(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch, priced=False)
    current_user["value"] = researcher_user(seeded.owner_id)
    base = {"name": "new study", "session_policy": VALID_SESSION_POLICY, "telemetry_policy": {"metadata_only": True}, "profile_ids": [str(seeded.profile_id)]}

    missing = client.post("/api/research/studies", json=base)
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["code"] == "BUDGET_REQUIRED"
    zero = client.post("/api/research/studies", json={**base, "default_budget_usd": "0"})
    assert zero.status_code == 422 and zero.json()["detail"]["code"] == "BUDGET_INVALID"
    unpriced = client.post("/api/research/studies", json={**base, "default_budget_usd": "5"})
    assert unpriced.status_code == 422 and unpriced.json()["detail"]["code"] == "BUDGET_PRICE_MISSING"
    assert unpriced.json()["detail"]["missing"][0]["model"] == MODEL

    with session_factory() as db:
        _price(db, seeded.connection_id)
    created = client.post("/api/research/studies", json={**base, "default_budget_usd": "5", "budget_warning_fraction": 0.5})
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    assert study["budget_policy"]["metered"] is True
    assert study["budget_policy"]["default_budget_micro_usd"] == 5_000_000
    assert study["budget_policy"]["warning_fraction"] == 0.5
    assert study["lifecycle_capabilities"]["budget_editable"] is True

    # A new enrollment is born with a balance at the study default.
    with session_factory() as db:
        assert ledger.balance_view(db, seeded.enrollment_id) is not None


def test_codex_only_study_ignores_budget_and_reports_unmetered(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch, framework="codex", priced=False)
    current_user["value"] = researcher_user(seeded.owner_id)
    created = client.post(
        "/api/research/studies",
        json={"name": "codex study", "session_policy": VALID_SESSION_POLICY, "telemetry_policy": {"metadata_only": True}, "profile_ids": [str(seeded.profile_id)]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["study"]["budget_policy"]["metered"] is False
    assert created.json()["study"]["budget_policy"]["default_budget_micro_usd"] == 0
    url = BUDGET.format(study=seeded.study_id)
    assert client.get(url).json()["metered"] is False
    assert client.patch(url, json={"default_budget_usd": "3"}).status_code == 409
    current_user["value"] = participant_user(seeded.account_id)
    mine = client.get("/api/research/participants/me").json()["enrollments"][0]
    assert mine["runtime"]["credentials"] == "own"


def test_provider_connection_prices_round_trip(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    with session_factory() as db:
        admin_id = seed_account(db, "admin@example.com", is_admin=True, can_research=True)
        researcher_id = seed_account(db, "researcher@example.com", can_research=True)
    current_user["value"] = admin_user(admin_id)
    payload = {
        "label": "priced", "base_url": "https://openrouter.ai/api/v1", "secret_ref": "OPENROUTER_API_KEY",
        "models": ["a", "b"], "is_active": True,
        "model_prices": {"a": {"input_usd_per_million": "0.5", "output_usd_per_million": "1.5", "cached_input_usd_per_million": "0.25"}},
    }
    created = client.post("/api/research/provider-connections", json=payload)
    assert created.status_code == 201, created.text
    connection = created.json()["connection"]
    assert connection["model_prices"]["a"]["input_usd_per_million"] == "0.500000"
    assert connection["model_prices"]["b"] is None
    assert connection["pricing"] == {"complete": False, "missing_models": ["b"]}
    connection_id = connection["connection_id"]

    unknown = client.post("/api/research/provider-connections", json={**payload, "label": "x", "model_prices": {"zzz": {"input_usd_per_million": "1", "output_usd_per_million": "1"}}})
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "PRICE_MODEL_UNKNOWN"
    negative = client.post("/api/research/provider-connections", json={**payload, "label": "y", "model_prices": {"a": {"input_usd_per_million": "-1", "output_usd_per_million": "1"}}})
    assert negative.status_code == 422

    # Omitted prices leave the stored prices unchanged; models dropped from the allowlist lose theirs.
    updated = client.put(f"/api/research/provider-connections/{connection_id}", json={k: v for k, v in payload.items() if k != "model_prices"})
    assert updated.status_code == 200 and updated.json()["connection"]["model_prices"]["a"] is not None
    completed = client.put(f"/api/research/provider-connections/{connection_id}", json={**payload, "model_prices": {"b": {"input_usd_per_million": "2", "output_usd_per_million": "3"}}})
    assert completed.json()["connection"]["pricing"]["complete"] is True
    shrunk = client.put(f"/api/research/provider-connections/{connection_id}", json={**payload, "models": ["a"], "model_prices": None})
    assert shrunk.status_code == 200 and set(shrunk.json()["connection"]["model_prices"]) == {"a"}

    # Researchers see prices (not secrets); writes stay admin-only.
    current_user["value"] = researcher_user(researcher_id)
    listed = client.get("/api/research/provider-connections").json()["connections"]
    mine = next(item for item in listed if item["connection_id"] == connection_id)
    assert mine["model_prices"]["a"]["output_usd_per_million"] == "1.500000"
    assert "secret_ref" not in mine and "base_url" not in mine
    assert client.put(f"/api/research/provider-connections/{connection_id}", json=payload).status_code == 403

    # An empty price map clears every price of the connection (admin).
    current_user["value"] = admin_user(admin_id)
    cleared = client.put(f"/api/research/provider-connections/{connection_id}", json={**payload, "models": ["a"], "model_prices": {}})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["connection"]["model_prices"] == {"a": None}, cleared.text
    assert cleared.json()["connection"]["pricing"] == {"complete": False, "missing_models": ["a"]}


def test_profile_payload_reports_model_priced(http_runtime, monkeypatch):
    client, session_factory, current_user = http_runtime
    seeded = Seeded(session_factory, monkeypatch, priced=False)
    current_user["value"] = researcher_user(seeded.owner_id)
    listed = client.get("/api/agent/profiles")
    assert listed.status_code == 200, listed.text
    profiles = listed.json() if isinstance(listed.json(), list) else listed.json().get("profiles", [])
    mine = next(item for item in profiles if item["profile_id"] == str(seeded.profile_id))
    assert mine["model_priced"] is False
    with session_factory() as db:
        _price(db, seeded.connection_id)
    listed = client.get("/api/agent/profiles")
    profiles = listed.json() if isinstance(listed.json(), list) else listed.json().get("profiles", [])
    assert next(item for item in profiles if item["profile_id"] == str(seeded.profile_id))["model_priced"] is True
