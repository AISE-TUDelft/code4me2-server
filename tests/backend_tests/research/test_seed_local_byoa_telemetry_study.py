"""The local BYOA telemetry seed follows the current research contract.

A seeded Goose study must bootstrap (gateway runtime bindings present), be
metered without failing closed (priced model, non-zero default budget) and,
for the tool-use arm, enforce Goose's tool selection at the research gateway.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from agents.tools import tools_for_framework
from research.study.agents.distributions import (
    missing_gateway_bindings,
    release_bindings,
    release_enforces_tools_at_gateway,
    validate_profile_configuration,
)

from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dev/seed_local_byoa_telemetry_study.py"
spec = importlib.util.spec_from_file_location("seed_local_byoa_telemetry_study", SCRIPT)
seed_script = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = seed_script  # dataclasses resolve annotations through it
spec.loader.exec_module(seed_script)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _profile(plan, release):
    return SimpleNamespace(release_id=release.release_id, name="seeded", temperature=None, **plan.profile_fields())


def _plan(framework="goose", *, gateway_tools=False, model="cohere/north-mini-code:free"):
    price_in, price_out = seed_script.default_price(model)
    return seed_script.StudyPlan(
        framework=framework, model=model, interaction=gateway_tools, gateway_tools=gateway_tools,
        budget_micro_usd=1_000_000, input_usd_per_million=price_in, output_usd_per_million=price_out,
    )


@pytest.mark.parametrize(
    ("environ", "allowed"),
    [
        ({}, False),
        ({"CODE4ME_DEV_SEED": "1", "DB_HOST": "db.example.org"}, False),
        ({"CODE4ME_DEV_SEED": "1", "DB_HOST": "localhost"}, True),
        ({"TEST_MODE": "true"}, True),
    ],
)
def test_seed_requires_an_explicit_local_development_database(environ, allowed):
    if allowed:
        seed_script.require_local_dev(environ)
    else:
        with pytest.raises(seed_script.SeedError):
            seed_script.require_local_dev(environ)


def test_goose_release_is_gateway_bound_and_its_profile_validates():
    plan = _plan()
    release = seed_script.build_release("goose", "1.51.0", gateway_tools=False, tested_at=NOW)

    validate_profile_configuration(_profile(plan, release), release)
    assert missing_gateway_bindings(release_bindings(release)) == []
    assert release_enforces_tools_at_gateway(release) is False
    assert release.adapter.adapter_id == "generic-acp"


def test_goose_tool_use_release_enforces_its_selection_at_the_gateway():
    plan = _plan(gateway_tools=True, model="openai/gpt-4.1-mini")
    release = seed_script.build_release("goose", "1.51.0", gateway_tools=True, tested_at=NOW)

    validate_profile_configuration(_profile(plan, release), release)
    assert release_enforces_tools_at_gateway(release) is True
    assert set(json.loads(plan.profile_fields()["tools_json"])) <= set(tools_for_framework("goose"))
    assert plan.profile_fields()["approval_policy"] == "per_step"


def test_codex_release_never_claims_gateway_enforcement():
    plan = _plan("codex")
    release = seed_script.build_release("codex", "0.10.0", gateway_tools=False, tested_at=NOW)

    validate_profile_configuration(_profile(plan, release), release)
    assert release_enforces_tools_at_gateway(release) is False
    with pytest.raises(seed_script.SeedError):
        seed_script.profile_bindings("codex", gateway_tools=True)


def test_default_price_is_zero_only_for_free_models():
    assert seed_script.default_price("cohere/north-mini-code:free") == (Decimal("0"), Decimal("0"))
    assert all(value > 0 for value in seed_script.default_price("openai/gpt-4.1-mini"))


def test_seeding_a_goose_tool_use_study_is_metered_and_idempotent(http_runtime):
    _, session_factory, _ = http_runtime
    plan = _plan(gateway_tools=True, model="openai/gpt-4.1-mini")
    release = seed_script.build_release("goose", "1.51.0", gateway_tools=True, tested_at=NOW)

    with session_factory() as session:
        session.execute(text("INSERT INTO public.config (config_data) VALUES ('{}')"))
        session.commit()
        first = seed_script.seed(session, plan, release, now=NOW)
        second = seed_script.seed(session, plan, release, now=NOW)

    assert first == second  # re-running resolves the same release, profile and study
    assert first["notes"] == []

    # A re-run asking for something else keeps what exists and says so.
    with session_factory() as session:
        third = seed_script.seed(
            session,
            seed_script.StudyPlan(**{**plan.__dict__, "budget_micro_usd": 5_000_000}),
            release,
            now=NOW,
        )
    assert third["budget_usd"] == "1.00"
    assert any("budget" in note for note in third["notes"])
    with session_factory() as session:
        study = session.execute(
            text("SELECT inference_budget_default_micro_usd, is_active FROM public.study WHERE study_id = :s"),
            {"s": uuid.UUID(first["study_id"])},
        ).mappings().one()
        price = session.execute(
            text("SELECT input_usd_per_million FROM public.provider_model_price WHERE model = :m"),
            {"m": plan.model},
        ).scalar_one()
        profile = session.execute(
            text("SELECT tools_json, framework_version FROM public.agent_profile WHERE profile_id = :p"),
            {"p": uuid.UUID(first["profile_id"])},
        ).mappings().one()
    assert study["inference_budget_default_micro_usd"] == 1_000_000 and study["is_active"] is True
    assert Decimal(price) == Decimal("0.40")
    assert json.loads(profile["tools_json"]) == list(seed_script.GOOSE_TOOL_USE_SELECTION)
    assert profile["framework_version"] == "goose"


def test_the_self_check_reports_an_agent_that_exits_before_answering():
    import time as _time

    started = _time.monotonic()
    with pytest.raises(seed_script.SeedError, match="exited before answering"):
        seed_script.check_agent([sys.executable, "-c", "pass"], [sys.executable, "--version"], {"PATH": "/usr/bin:/bin"})
    assert _time.monotonic() - started < 10

