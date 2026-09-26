"""Real installed agents against a local provider; separate from the IDE layer."""
from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Dict, Optional

from . import acp_probe, real_agents, workflow
from .agent_provider import GATEWAY_CHAT_ROUTE, AgentProvider
from .config import AgentProbeReason
from .steps import StepResult

if TYPE_CHECKING:
    from pathlib import Path

#: Goose 1.51 makes at most two provider calls per prompt turn: the reply and
#: the session-description (title) call. More gateway requests than that in
#: quota mode means the agent retried a refusal it must not retry.
GOOSE_CALLS_PER_TURN = 2

#: A deliberately wrong Goose config: another provider and a dead host. The
#: study launch must override it through the environment (the plugin's runtime
#: bindings), or the probe fails; this is the bypass hardening the research
#: proxy relies on.
POISONED_GOOSE_CONFIG = (
    "GOOSE_PROVIDER: anthropic\n"
    "GOOSE_MODEL: poisoned-model\n"
    "OPENAI_HOST: http://127.0.0.1:9\n"
    "ANTHROPIC_HOST: http://127.0.0.1:9\n"
)


def _poison_goose_home(home: "Path") -> None:
    config_dir = home / ".config" / "goose"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(POISONED_GOOSE_CONFIG, encoding="utf-8")


def probe(framework, run_path: Path, *, executable=None, timeout=120,
          command=None, package=None, argv=None, home=None, quota_exhausted=False):
    """Require a fresh provider receipt AND its unique answer over real ACP.

    Goose runs in the *gateway shape* a study arm receives: ``OPENAI_HOST`` is
    the provider origin, ``OPENAI_BASE_PATH`` the research gateway path, the
    API key a random bearer the provider must see back, ``GOOSE_PROVIDER``
    selected through the environment and ``GOOSE_PATH_ROOT`` isolating Goose's
    own state, while the isolated home carries a poisoned ``config.yaml``.

    With ``quota_exhausted`` the provider answers the research gateway's
    ``402 quota_exhausted`` refusal; the probe then passes only when the agent
    reports a typed ``quota`` block after no more than its normal calls for
    one turn (``GOOSE_CALLS_PER_TURN``; no retry storm), which is how a used-up
    participant budget must look.
    """
    bearer = "c4m-" + secrets.token_urlsafe(24) if framework == "goose" else None
    provider = AgentProvider(expected_bearer=bearer, quota_exhausted=quota_exhausted)
    provider.start()
    try:
        origin = provider.base_url
        extra = {"OPENAI_API_KEY": bearer or "e2e-placeholder", "NO_PROXY": "127.0.0.1,localhost"}
        prepare_home = None
        if framework == "goose":
            extra.update(GOOSE_PROVIDER="openai", GOOSE_MODEL="e2e-stub-model",
                         OPENAI_HOST=origin, OPENAI_BASE_PATH=GATEWAY_CHAT_ROUTE.lstrip("/"),
                         GOOSE_DISABLE_KEYRING="1")
            # The plugin's state_dir binding: Goose's own config/data/state live
            # in a directory of ours, not in the (poisoned) participant home.
            extra["GOOSE_PATH_ROOT"] = str(run_path / "goose-root")
            route = GATEWAY_CHAT_ROUTE
            prepare_home = _poison_goose_home
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
            prepare_home=prepare_home,
        )
        requests = provider.requests_since(before)
        gateway_requests = [item for item in requests if item.get("route") == route]
        protocol_extra = {
            "provider": "local deterministic fixture",
            "provider_requests": requests,
            "expected_answer_received": result.passed,
            "gateway_shape": framework == "goose",
            "quota_exhausted_mode": bool(quota_exhausted),
            "coverage": "installed agent ACP transport; IDE and backend covered separately",
        }
        if quota_exhausted:
            # A pass here is the typed quota block after exactly one request.
            blocked_on_quota = result.blocked and result.reason == AgentProbeReason.QUOTA.value
            if blocked_on_quota and 1 <= len(gateway_requests) <= GOOSE_CALLS_PER_TURN:
                return real_agents.ProbeResult(
                    **{**result.__dict__, "status": "PASS", "reason": None,
                       "detail": "the agent surfaced the gateway's 402 quota refusal without retrying",
                       "protocol": {**result.protocol, **protocol_extra}},
                )
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.PROTOCOL,
                (
                    f"expected a typed quota block after at most {GOOSE_CALLS_PER_TURN} gateway "
                    f"requests (one turn, no retries), got status={result.status} "
                    f"reason={result.reason} requests={len(gateway_requests)}"
                ),
                identity=result.identity, checks=result.checks,
                protocol={**result.protocol, **protocol_extra},
            )
        if result.passed and not gateway_requests:
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.PROTOCOL,
                "No request reached this probe's local provider", identity=result.identity,
                checks=result.checks,
            )
        if result.passed and framework == "goose" and not all(
            item.get("auth_matched") is True for item in gateway_requests
        ):
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.PROTOCOL,
                "The agent did not present the gateway bearer it was given",
                identity=result.identity, checks=result.checks,
            )
        return real_agents.ProbeResult(
            **{**result.__dict__, "protocol": {**result.protocol, **protocol_extra}},
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
