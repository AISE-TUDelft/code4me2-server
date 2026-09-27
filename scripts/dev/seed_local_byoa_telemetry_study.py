#!/usr/bin/env python3
"""Create one local-only Goose or Codex (BYOA) telemetry test study.

A development helper for live telemetry checks with a participant-installed
agent. It follows the current research contract:

* the installed agent must answer ``--version`` and an ACP ``initialize``
  handshake before a release is recorded;
* a Goose release carries the research inference gateway runtime bindings
  (the ``participant-release.example.json`` contract), so bootstrap never
  blocks with ``INFERENCE_GATEWAY_UNBOUND``;
* Goose arms are metered: the model gets a price on the development provider
  connection and the study a default participant budget, because both fail
  closed when missing;
* ``--tool-use-test`` enforces the Goose tool selection at the research
  inference gateway (Goose reads no tool setting from its environment);
* a Codex arm runs on the participant's own login and is never study-funded.

It never enrolls or consents a participant: the test account joins through the
normal web flow with the printed join code.

Usage (from ``code4me2-server/``, development only)::

    CODE4ME_DEV_SEED=1 PYTHONPATH=src python scripts/dev/seed_local_byoa_telemetry_study.py goose
    CODE4ME_DEV_SEED=1 PYTHONPATH=src python scripts/dev/seed_local_byoa_telemetry_study.py goose --tool-use-test
    CODE4ME_DEV_SEED=1 PYTHONPATH=src python scripts/dev/seed_local_byoa_telemetry_study.py codex

``TEST_MODE=true`` is accepted in place of ``CODE4ME_DEV_SEED=1``; a database
host other than the local machine is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

REPO = Path(__file__).resolve().parents[2]

#: Goose 1.51's developer tools as a study gateway call offers them (observed
#: 2026-09-27); the tool-use arm allows these and withholds the rest.
GOOSE_TOOL_USE_SELECTION = ("shell", "edit", "write", "tree")

DEFAULT_MODEL = "cohere/north-mini-code:free"
TOOL_USE_MODEL = "openai/gpt-4.1-mini"
FREE_TOOL_USE_MODEL = "inclusionai/ling-3.0-flash-sante:free"
DEFAULT_BUDGET_USD = Decimal("1.00")

#: The canonical Goose runtime bindings (``participant-release.example.json``).
GOOSE_GATEWAY_BINDINGS: tuple[dict[str, Any], ...] = (
    {"field": "inference_gateway_host", "transport": "env", "key": "OPENAI_HOST"},
    {"field": "inference_gateway_base_path", "transport": "env", "key": "OPENAI_BASE_PATH"},
    {"field": "inference_gateway_credential", "transport": "env", "key": "OPENAI_API_KEY"},
    {
        "field": "provider_kind",
        "transport": "env",
        "key": "GOOSE_PROVIDER",
        "value_map": {"openai_compatible": "openai"},
    },
    {"field": "state_dir", "transport": "env", "key": "GOOSE_PATH_ROOT"},
)


class SeedError(RuntimeError):
    """A precondition of the seed failed; nothing further is written."""


def require_local_dev(environ: Mapping[str, str]) -> None:
    """Refuse anything but an explicit, local development database."""
    if environ.get("CODE4ME_DEV_SEED") != "1" and environ.get("TEST_MODE", "").lower() != "true":
        raise SeedError("set CODE4ME_DEV_SEED=1 (or TEST_MODE=true) to seed a development database")
    if environ.get("DB_HOST") not in (None, "", "localhost", "127.0.0.1", "::1"):
        raise SeedError("refusing to seed a database that is not on this machine")


def profile_bindings(framework: str, *, gateway_tools: bool) -> list[dict[str, Any]]:
    """The release's profile-field bindings (plus Goose's gateway runtime bindings)."""
    if framework == "goose":
        bindings: list[dict[str, Any]] = [
            {"field": "model", "transport": "env", "key": "GOOSE_MODEL"},
            {"field": "max_steps", "transport": "env", "key": "GOOSE_MAX_TURNS"},
            {
                "field": "approval_policy",
                "transport": "env",
                "key": "GOOSE_MODE",
                "value_map": {"suggestion_only": "chat", "per_step": "approve", "auto": "auto"},
            },
            *[dict(binding) for binding in GOOSE_GATEWAY_BINDINGS],
        ]
        if gateway_tools:
            bindings.append(
                {"field": "tools", "transport": "gateway", "key": "tool_allowlist", "format": "json"}
            )
        return bindings
    if gateway_tools:
        raise SeedError("only a Goose arm can have its tools enforced at the research gateway")
    return [
        {"field": "model", "transport": "env", "key": "CODEX_MODEL"},
        {"field": "max_steps", "transport": "env", "key": "CODEX_MAX_TURNS"},
        {
            "field": "approval_policy",
            "transport": "env",
            "key": "INITIAL_AGENT_MODE",
            "value_map": {"suggestion_only": "read-only", "per_step": "agent", "auto": "agent-full-access"},
        },
    ]


def build_release(
    framework: str,
    version: str,
    *,
    gateway_tools: bool,
    tested_at: datetime,
    platform: Optional[tuple[str, str]] = None,
) -> Any:
    """A qualified BYOA release record for the checked, installed agent."""
    from research.study.agents.enums import DistributionMode
    from research.study.agents.models import (
        AdapterRef,
        AgentConfigBinding,
        AgentReleaseV1,
        ReleaseTests,
    )
    from research.study.agents.registry import derive_qualification_status

    sys.path.insert(0, str(REPO))
    from scripts.dev.seed_research_study import detect_arch, detect_os  # noqa: E402

    os_name, arch = platform or (detect_os(), detect_arch())
    command, args, package = ("goose", ["acp"], "goose") if framework == "goose" else ("codex-acp", [], "codex")
    bindings = profile_bindings(framework, gateway_tools=gateway_tools)
    evidence = {
        "framework": framework, "version": version, "command": command, "args": args,
        "bindings": bindings, "tested_at": tested_at.isoformat(),
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    release_id = f"local-telemetry-{framework}-{version}" + ("-gateway-tools-v1" if gateway_tools else "")
    release = AgentReleaseV1(
        agent_id=framework, release_id=release_id, version=version,
        source_manifest_digest=digest, distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_command=command, agent_command_args=args, agent_package=package,
        byoa_config=[AgentConfigBinding(**binding) for binding in bindings],
        # Base ACP normalisation only; the proxy accepts this id.
        adapter=AdapterRef(adapter_id="generic-acp", version="1.0.0"),
        # The self-check ran on this machine, so the release qualifies for it.
        tests=[ReleaseTests(os=os_name, arch=arch, self_check="PASS", acp_initialize="PASS", ran_at=tested_at)],
    )
    # Qualification is what the registry derives from the recorded tests.
    status = derive_qualification_status(release.model_dump(mode="json"))
    return release.model_copy(update={"qualification_status": status})


@dataclass(frozen=True)
class StudyPlan:
    framework: str
    model: str
    interaction: bool
    gateway_tools: bool
    budget_micro_usd: int
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    @property
    def kind(self) -> str:
        if self.gateway_tools:
            return "interaction-gateway-tools"
        return "interaction" if self.interaction else "telemetry"

    def profile_fields(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "framework_version": self.framework,
            "tools_json": json.dumps(list(GOOSE_TOOL_USE_SELECTION)) if self.gateway_tools else "[]",
            "approval_policy": "per_step" if self.interaction else "suggestion_only",
            "max_steps": 8 if self.interaction else 1,
        }


def default_price(model: str) -> tuple[Decimal, Decimal]:
    """A development price: free models cost nothing, others a placeholder."""
    if model.endswith(":free"):
        return Decimal("0"), Decimal("0")
    return Decimal("0.40"), Decimal("1.60")


def check_agent(command: Sequence[str], version_command: Sequence[str], environment: Mapping[str, str],
                cwd: Optional[str] = None) -> str:
    """The agent's version, after a successful ACP ``initialize`` handshake."""
    version = subprocess.run(
        list(version_command), check=True, capture_output=True, text=True, timeout=30,
        env=dict(environment), cwd=cwd,
    ).stdout.strip()
    if not version:
        raise SeedError("the agent's version check printed nothing")
    process = subprocess.Popen(
        list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=dict(environment), cwd=cwd,
    )
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def read_lines() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)  # end of stream: the agent exited or closed stdout

    reader = threading.Thread(target=read_lines, daemon=True)
    try:
        assert process.stdin is not None and process.stdout is not None
        reader.start()
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {},
                       "clientInfo": {"name": "code4me-local-release-check", "version": "1"}},
        }) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise SeedError("ACP initialize did not complete within 30 s") from None
            if line is None:
                raise SeedError("the agent exited before answering ACP initialize")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                raise SeedError("the agent wrote a non-JSON line on its ACP stdout") from error
            if response.get("id") == 1:
                if (response.get("result") or {}).get("protocolVersion") != 1:
                    raise SeedError("ACP initialize did not negotiate protocol version 1")
                return version.rsplit(" ", 1)[-1]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def seed(session: Any, plan: StudyPlan, release: Any, *, now: datetime) -> dict[str, Any]:
    """Write the release, priced connection, profile and active study (idempotent)."""
    from sqlalchemy import select

    from database import db_schemas
    from research.study import lifecycle as study_lifecycle
    from research.study.agents import store as agent_store
    from research.study.agents.distributions import validate_profile_configuration
    from research.study.agents.enums import QualificationStatus
    from research.study.protocol import store as study_store

    sys.path.insert(0, str(REPO))
    from scripts.dev.seed_research_study import (  # noqa: E402 - repository-root script
        FIXED_SCHEDULE_END,
        FIXED_SCHEDULE_START,
        ensure_dev_owner,
        ensure_dev_provider_connection,
    )

    owner = ensure_dev_owner(
        session, f"telemetry-{plan.framework}-owner@example.com",
        password=secrets.token_urlsafe(32), name=f"Local {plan.framework.title()} Telemetry Owner",
    )
    connection = ensure_dev_provider_connection(session)
    if not connection.is_active:
        raise SeedError("the development provider connection is inactive")
    models = json.loads(connection.models_json or "[]")
    if plan.model not in models:
        connection.models_json = json.dumps([*models, plan.model])
        session.add(connection)
    if session.get(db_schemas.ProviderModelPrice, (connection.connection_id, plan.model)) is None:
        session.add(db_schemas.ProviderModelPrice(
            connection_id=connection.connection_id, model=plan.model,
            input_usd_per_million=plan.input_usd_per_million,
            output_usd_per_million=plan.output_usd_per_million,
            updated_by="seed_local_byoa_telemetry_study",
        ))
    session.commit()

    stored = agent_store.get_release(session, release.release_id)
    if stored is None:
        stored = agent_store.upsert_release(session, release)
    if stored.status != QualificationStatus.QUALIFIED.value:
        raise SeedError(f"release {release.release_id} is not qualified: {stored.status}")

    profile_name = f"local-{plan.kind}-{plan.framework}"
    profile = session.execute(select(db_schemas.AgentProfile).where(
        db_schemas.AgentProfile.owner_user_id == owner.user_id,
        db_schemas.AgentProfile.name == profile_name,
    )).scalars().first()
    if profile is None:
        profile = db_schemas.AgentProfile(
            profile_id=uuid.uuid4(), owner_user_id=owner.user_id, name=profile_name,
            release_id=release.release_id, connection_id=connection.connection_id,
            is_active=True, **plan.profile_fields(),
        )
        # The same invariant the profile API and the study freeze apply.
        validate_profile_configuration(profile, release)
        session.add(profile)
        session.commit()
        session.refresh(profile)

    study_id = uuid.uuid5(uuid.NAMESPACE_URL, f"code4me2://local-{plan.kind}/{plan.framework}")
    if study_store.get_study(session, study_id) is None:
        study_store.create_study(
            session, study_id=study_id,
            name=f"Local {plan.framework.title()} {plan.kind.replace('-', ' ').title()} Verification",
            description="Development-only agent telemetry verification; not a research study.",
            created_by=owner.user_id, starts_at=FIXED_SCHEDULE_START, ends_at=FIXED_SCHEDULE_END,
            is_research=True,
            research_config_json={
                "telemetry_policy": {"metadata_only": True},
                "session_policy": {"idle_timeout_seconds": 900, "resume_grace_seconds": 300,
                                   "heartbeat_seconds": 30},
            },
            join_code=study_lifecycle.allocate_join_code(session),
            profile_ids=[profile.profile_id],
            now=now,
            inference_budget_default_micro_usd=plan.budget_micro_usd,
        )
    study_store.set_study_active(session, study_id, True)
    study = study_store.get_study(session, study_id)
    stored_budget = session.get(db_schemas.Study, study_id).inference_budget_default_micro_usd
    summary = {
        "study_id": str(study_id), "join_code": study.join_code, "release_id": profile.release_id,
        "profile_id": str(profile.profile_id), "model": profile.model,
        "budget_usd": f"{Decimal(stored_budget or 0) / Decimal(1_000_000):.2f}",
    }
    # A re-run keeps what exists; say so instead of printing values it did not apply.
    summary["notes"] = [
        f"existing {what} kept: {stored!r} (requested {wanted!r})"
        for what, stored, wanted in (
            ("profile release", profile.release_id, release.release_id),
            ("profile model", profile.model, plan.model),
            ("study default budget (micro-USD)", stored_budget, plan.budget_micro_usd),
        )
        if stored != wanted
    ]
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("framework", choices=("goose", "codex"))
    parser.add_argument("--interaction-test", action="store_true",
                        help="multi-step study with per-step approval")
    tools = parser.add_mutually_exclusive_group()
    tools.add_argument("--tool-use-test", action="store_true",
                       help=f"Goose: gateway-enforced tools {list(GOOSE_TOOL_USE_SELECTION)} on {TOOL_USE_MODEL}")
    tools.add_argument("--free-tool-use-test", action="store_true",
                       help=f"as --tool-use-test on the free {FREE_TOOL_USE_MODEL}")
    parser.add_argument("--model", default=None, help="override the model")
    parser.add_argument("--budget-usd", type=Decimal, default=DEFAULT_BUDGET_USD,
                        help="default participant budget (Goose arms are metered)")
    parser.add_argument("--goose", default=None, help="Goose executable (default: goose on PATH)")
    parser.add_argument("--codex-acp", default=None,
                        help="the codex-acp executable the release launches (default: codex-acp on PATH)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    gateway_tools = args.tool_use_test or args.free_tool_use_test
    if gateway_tools and args.framework != "goose":
        parser.error("--tool-use-test/--free-tool-use-test need the goose framework")
    model = args.model or (
        FREE_TOOL_USE_MODEL if args.free_tool_use_test else TOOL_USE_MODEL if args.tool_use_test else DEFAULT_MODEL
    )
    price_in, price_out = default_price(model)
    plan = StudyPlan(
        framework=args.framework, model=model, interaction=args.interaction_test or gateway_tools,
        gateway_tools=gateway_tools, budget_micro_usd=int(args.budget_usd * 1_000_000),
        input_usd_per_million=price_in, output_usd_per_million=price_out,
    )

    from dotenv import load_dotenv

    load_dotenv()
    require_local_dev(os.environ)

    environment = os.environ.copy()
    if args.framework == "goose":
        goose = args.goose or shutil.which("goose")
        if not goose:
            raise SeedError("no goose executable on PATH; pass --goose")
        version = check_agent([goose, "acp"], [goose, "--version"], environment)
    else:
        # The release launches `codex-acp`; check that very executable.
        codex_acp = args.codex_acp or shutil.which("codex-acp")
        if not codex_acp:
            raise SeedError("no codex-acp executable on PATH; install one (or link the vendored "
                            "adapter) or pass --codex-acp")
        environment.update(CODEX_MODEL=model)
        version = check_agent([codex_acp], [codex_acp, "--version"], environment)
    print(f"{args.framework} {version}: version check and ACP initialize passed")

    from App import App

    session = App.get_instance().get_db_session()
    try:
        now = datetime.now(timezone.utc)
        summary = seed(session, plan, build_release(args.framework, version, gateway_tools=gateway_tools, tested_at=now), now=now)
    finally:
        session.close()
    for note in summary.pop("notes"):
        print(f"note: {note}")
    for key, value in summary.items():
        print(f"{key}={value}")
    print("No participant was enrolled or consented; join with the code above.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as error:
        print(f"seed refused: {error}", file=sys.stderr)
        sys.exit(2)
