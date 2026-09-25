"""Real installed agents against a local provider; separate from the IDE layer."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, Optional

from . import acp_probe, real_agents, workflow
from .agent_provider import AgentProvider
from .config import AgentProbeReason
from .steps import StepResult

if TYPE_CHECKING:
    from pathlib import Path


def probe(framework, run_path: Path, *, executable=None, timeout=120,
          command=None, package=None, argv=None, home=None):
    """Require a fresh provider receipt AND its unique answer over real ACP."""
    provider = AgentProvider()
    provider.start()
    try:
        origin = provider.base_url
        extra = {"OPENAI_API_KEY": "e2e-placeholder", "NO_PROXY": "127.0.0.1,localhost"}
        if framework == "goose":
            extra.update(GOOSE_PROVIDER="openai", GOOSE_MODEL="e2e-stub-model",
                         OPENAI_HOST=origin, OPENAI_BASE_PATH="v1/chat/completions",
                         OPENAI_BASE_URL=origin + "/v1", GOOSE_DISABLE_KEYRING="1")
            route = "/v1/chat/completions"
        elif framework == "codex":
            extra.update(CODEX_PROXY_URL=origin + "/v1", CODEX_MODEL="e2e-stub-model",
                         OPENAI_BASE_URL=origin + "/v1")
            route = "/v1/responses"
        else:
            raise ValueError(f"unsupported framework: {framework}")
        before = provider.request_count()
        result = acp_probe.run_probe(
            framework, run_dir=run_path, executable=executable, timeout=timeout,
            command=command, package=package, argv=argv, home=home,
            prompt="Say hello in one short sentence. Do not use tools.",
            env_extra=extra, expected_substring=provider.token,
        )
        requests = provider.requests_since(before)
        if result.passed and not any(item.get("route") == route for item in requests):
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.PROTOCOL,
                "No request reached this probe's local provider", identity=result.identity,
                checks=result.checks,
            )
        return real_agents.ProbeResult(
            **{**result.__dict__, "protocol": {**result.protocol,
                "provider": "local deterministic fixture",
                "provider_requests": requests,
                "expected_answer_received": result.passed,
                "coverage": "installed agent ACP transport; IDE and backend covered separately"}},
        )
    finally:
        provider.stop()


def run(scenario, run_path: Path, *, executables: Optional[Dict[str, str]] = None) -> int:
    """Attempt both independent agents; neither missing nor blocked can pass.

    ``executables`` carries what the prerequisite step resolved (the vendored
    Codex ACP adapter); anything absent is discovered on the host.
    """
    started = time.monotonic()
    results = []
    for framework in ("goose", "codex"):
        try:
            result = probe(framework, run_path / "native-agents" / framework,
                           executable=(executables or {}).get(framework),
                           timeout=scenario.timeouts.step_seconds)
            results.append(result.to_dict())
        except Exception as error:
            results.append({"framework": framework, "status": "FAIL",
                            "detail": real_agents.redact_failure_text(str(error))})
    passed = all(item["status"] == "PASS" for item in results)
    status = "PASS" if passed else "FAIL" if any(item["status"] == "FAIL" for item in results) else "BLOCKED"
    workflow._record_plugin_step(run_path, scenario, StepResult(
        "agents_test", status, int((time.monotonic() - started) * 1000),
        {"agents": results}, "" if passed else "Inspect agents_test results for the failed setup stage",
    ))
    return 0 if passed else 1
