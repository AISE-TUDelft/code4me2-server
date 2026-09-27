#!/usr/bin/env python3
"""Create one isolated, local-only Goose or Codex telemetry study.

This development helper runs the installed agent's version and ACP initialize
checks before recording a BYOA release. It deliberately does not enroll or
consent a participant; the test account must use the normal join flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import selectors
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def require_local_dev() -> None:
    if os.environ.get("CODE4ME_DEV_SEED") != "1":
        raise SystemExit("Set CODE4ME_DEV_SEED=1 for local development seeding")
    if os.environ.get("DB_HOST") not in (None, "localhost", "127.0.0.1"):
        raise SystemExit("Refusing to seed a non-local database")
    if os.environ.get("DB_NAME") != "code4meResearchDev":
        raise SystemExit("Refusing to seed a database other than code4meResearchDev")


def check_agent(command: list[str], version_command: list[str], environment: dict[str, str]) -> str:
    version = subprocess.run(version_command, check=True, capture_output=True, text=True, timeout=20, env=environment).stdout.strip()
    if not version:
        raise RuntimeError("agent version check produced no version")
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=environment,
    )
    try:
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {},
                       "clientInfo": {"name": "code4me-local-release-check", "version": "1"}},
        }) + "\n")
        process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if not selector.select(timeout=1):
                continue
            line = process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == 1:
                if response.get("result", {}).get("protocolVersion") != 1:
                    raise RuntimeError("ACP initialize did not negotiate protocol 1")
                return version
        raise RuntimeError("ACP initialize did not complete")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("framework", choices=("goose", "codex"))
    parser.add_argument(
        "--interaction-test",
        action="store_true",
        help="Create a separate local study with multi-step, per-step-approved agent actions",
    )
    parser.add_argument(
        "--tool-use-test",
        action="store_true",
        help="Create a separate Goose interaction study pinned to a tool-capable OpenRouter model",
    )
    parser.add_argument(
        "--free-tool-use-test",
        action="store_true",
        help="Create a separate Goose interaction study pinned to a tested free tool-capable model",
    )
    args = parser.parse_args()
    if args.tool_use_test and args.free_tool_use_test:
        parser.error("Choose only one tool-use test model")
    if (args.tool_use_test or args.free_tool_use_test) and (args.framework != "goose" or not args.interaction_test):
        parser.error("Tool-use tests require goose --interaction-test")
    model = (
        "inclusionai/ling-3.0-flash-sante:free" if args.free_tool_use_test else
        "openai/gpt-4.1-mini" if args.tool_use_test else
        "cohere/north-mini-code:free"
    )
    gateway_tool_use = args.tool_use_test or args.free_tool_use_test

    from dotenv import load_dotenv
    load_dotenv()
    require_local_dev()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from App import App
    from database import crud, db_schemas
    from research.study.agents import store as agent_store
    from research.study.agents.enums import DistributionMode, QualificationStatus
    from research.study.agents.models import AdapterRef, AgentConfigBinding, AgentReleaseV1, ReleaseTests
    from research.study import lifecycle as study_lifecycle
    from research.study.protocol import store as study_store
    from scripts.dev.seed_research_study import FIXED_SCHEDULE_START, FIXED_SCHEDULE_END, ensure_dev_owner
    from sqlalchemy import select

    client = repo.parent / "code4me2"
    source = client / "dev/codex-acp-proxy/codex-acp"
    environment = os.environ.copy()
    if args.framework == "goose":
        command = ["/opt/homebrew/bin/goose", "acp"]
        version_command = [command[0], "--version"]
        environment.update(GOOSE_PROVIDER="openai", GOOSE_MODEL=model,
                           OPENAI_BASE_URL="https://openrouter.ai/api/v1",
                           OPENAI_API_KEY="local-release-probe")
        release_command, release_args, package = "goose", ["acp"], "goose"
        bindings = [
            AgentConfigBinding(field="model", transport="env", key="GOOSE_MODEL"),
            AgentConfigBinding(field="max_steps", transport="env", key="GOOSE_MAX_TURNS"),
            AgentConfigBinding(field="approval_policy", transport="env", key="GOOSE_MODE",
                               value_map={"suggestion_only": "chat", "per_step": "approve", "auto": "auto"}),
        ]
        if gateway_tool_use:
            bindings.append(AgentConfigBinding(
                field="tools", transport="gateway", key="tool_allowlist", format="json"
            ))
    else:
        command = [str(source / "node_modules/.bin/tsx"), str(source / "src/index.ts")]
        version_command = command + ["--version"]
        environment.update(CODEX_PROXY_URL="http://127.0.0.1:1/v1",
                           OPENAI_API_KEY="local-release-probe", CODEX_MODEL=model)
        release_command, release_args, package = "codex-acp", [], "codex"
        bindings = [
            AgentConfigBinding(field="model", transport="env", key="CODEX_MODEL"),
            AgentConfigBinding(field="max_steps", transport="env", key="CODEX_MAX_TURNS"),
            AgentConfigBinding(field="approval_policy", transport="env", key="INITIAL_AGENT_MODE",
                               value_map={"suggestion_only": "read-only", "per_step": "agent", "auto": "agent-full-access"}),
        ]
    version = check_agent(command, version_command, environment)
    if args.framework == "codex":
        version = version.rsplit(" ", 1)[-1]
    print(f"{args.framework} self-check and ACP initialize passed (version {version})")

    app = App.get_instance()
    session = app.get_db_session()
    try:
        owner = ensure_dev_owner(
            session, f"telemetry-{args.framework}-owner@example.com",
            password=secrets.token_urlsafe(32), name=f"Local {args.framework.title()} Telemetry Owner",
        )
        connection = crud.get_provider_connection_by_label(session, "dev-openrouter")
        if connection is None or not connection.is_active:
            raise RuntimeError("dev-openrouter provider connection is missing or inactive")
        release_id = f"local-telemetry-{args.framework}-{version}"
        if gateway_tool_use:
            release_id += "-gateway-tools-v1"
        release = agent_store.get_release(session, release_id)
        if release is None:
            tested_at = datetime.now(timezone.utc)
            evidence = {
                "framework": args.framework, "version": version,
                "command": release_command, "args": release_args,
                "bindings": [item.model_dump(mode="json") for item in bindings],
                "tested_at": tested_at.isoformat(),
            }
            digest = "sha256:" + hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
            release = agent_store.upsert_release(session, AgentReleaseV1(
                agent_id=args.framework, release_id=release_id, version=version,
                source_manifest_digest=digest, distribution_mode=DistributionMode.BYOA_EXTERNAL,
                agent_command=release_command, agent_command_args=release_args,
                agent_package=package, byoa_config=bindings,
                adapter=AdapterRef(adapter_id="generic-acp", version="1.0.0"),
                tests=[ReleaseTests(os="macos", arch="arm64", self_check="PASS",
                                    acp_initialize="PASS", ran_at=tested_at)],
            ))
        if release.status != QualificationStatus.QUALIFIED.value:
            raise RuntimeError(f"release is not qualified: {release.status}")

        study_kind = (
            "interaction-free-native-gateway-tools" if args.free_tool_use_test else
            "interaction-native-gateway-tools" if args.tool_use_test else
            "interaction" if args.interaction_test else "telemetry"
        )
        profile_name = f"local-{study_kind}-{args.framework}"
        profile = session.execute(select(db_schemas.AgentProfile).where(
            db_schemas.AgentProfile.owner_user_id == owner.user_id,
            db_schemas.AgentProfile.name == profile_name,
        )).scalars().first()
        if profile is None:
            profile = db_schemas.AgentProfile(
                profile_id=uuid.uuid4(), owner_user_id=owner.user_id,
                name=profile_name, model=model,
                framework_version=args.framework, release_id=release_id,
                connection_id=connection.connection_id,
                tools_json=json.dumps(["read", "edit"]) if gateway_tool_use else "[]",
                approval_policy="per_step" if args.interaction_test else "suggestion_only",
                max_steps=8 if args.interaction_test else 1, is_active=True,
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)

        study_id = uuid.uuid5(uuid.NAMESPACE_URL, f"code4me2://local-{study_kind}/{args.framework}")
        study = study_store.get_study(session, study_id)
        if study is None:
            study = study_store.create_study(
                session, study_id=study_id,
                name=f"Local {args.framework.title()} {study_kind.title()} Verification",
                description="Development-only agent interaction verification; not a research study."
                if args.interaction_test else
                "Development-only agent telemetry verification; not a research study.",
                created_by=owner.user_id, starts_at=FIXED_SCHEDULE_START,
                ends_at=FIXED_SCHEDULE_END, is_research=True,
                research_config_json={
                    "telemetry_policy": {"metadata_only": True},
                    "session_policy": {"idle_timeout_seconds": 900,
                                       "resume_grace_seconds": 300,
                                       "heartbeat_seconds": 30},
                    "profile_ids": [str(profile.profile_id)],
                },
                join_code=study_lifecycle.allocate_join_code(session),
                profile_ids=[profile.profile_id],
            )
        study_store.set_study_active(session, study_id, True)
        study = study_store.get_study(session, study_id)
        print(f"study_id={study_id} release_id={release_id} profile_id={profile.profile_id}")
        print(f"join_code={study.join_code}")
        print("No participant was enrolled or consented by this script.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
