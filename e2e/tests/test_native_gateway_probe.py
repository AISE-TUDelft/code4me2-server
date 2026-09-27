"""The Goose probe runs in the research gateway shape and understands a 402."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from code4me_e2e import acp_probe, native_agents, real_agents
from code4me_e2e.agent_provider import GATEWAY_CHAT_ROUTE
from code4me_e2e.config import AgentProbeReason


def _post(env, *, bearer, stream=False):
    """Post one Chat Completions request the way Goose would, from its env."""
    url = env["OPENAI_HOST"].rstrip("/") + "/" + env["OPENAI_BASE_PATH"]
    request = Request(
        url, data=json.dumps({"model": env.get("GOOSE_MODEL"), "stream": stream, "messages": []}).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {bearer}"} if bearer else {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback fixture
            return response.status
    except HTTPError as error:
        return error.code


class GatewayShapeProbeTest(unittest.TestCase):
    def _run(self, side_effect, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, patch.object(acp_probe, "run_probe", side_effect=side_effect):
            return native_agents.probe("goose", Path(tmp), **kwargs)

    def test_goose_env_is_the_gateway_shape_and_the_home_is_poisoned(self):
        seen = {}

        def fake_run_probe(framework, **kwargs):
            env = kwargs["env_extra"]
            seen.update(env)
            with tempfile.TemporaryDirectory() as home:
                kwargs["prepare_home"](Path(home))
                seen["poisoned"] = (Path(home) / ".config" / "goose" / "config.yaml").read_text()
            self.assertEqual(200, _post(env, bearer=env["OPENAI_API_KEY"]))
            return real_agents.ProbeResult.ok(framework)

        result = self._run(fake_run_probe)
        self.assertTrue(result.passed, result.detail)
        self.assertEqual("openai", seen["GOOSE_PROVIDER"])
        self.assertEqual(GATEWAY_CHAT_ROUTE.lstrip("/"), seen["OPENAI_BASE_PATH"])
        self.assertTrue(seen["OPENAI_API_KEY"].startswith("c4m-"))
        self.assertNotIn("OPENAI_BASE_URL", seen)
        self.assertTrue(seen["GOOSE_PATH_ROOT"].endswith("goose-root"))
        self.assertIn("GOOSE_PROVIDER: anthropic", seen["poisoned"])
        self.assertTrue(result.protocol["gateway_shape"])
        self.assertTrue(all(item["auth_matched"] for item in result.protocol["provider_requests"]))

    def test_a_missing_bearer_blocks_the_probe(self):
        def fake_run_probe(framework, **kwargs):
            self.assertEqual(200, _post(kwargs["env_extra"], bearer=None))
            return real_agents.ProbeResult.ok(framework)

        result = self._run(fake_run_probe)
        self.assertEqual("BLOCKED", result.status)
        self.assertIn("gateway bearer", result.detail)

    def test_quota_mode_passes_only_on_a_typed_quota_block_after_one_request(self):
        def one_refused_request(framework, **kwargs):
            env = kwargs["env_extra"]
            self.assertEqual(402, _post(env, bearer=env["OPENAI_API_KEY"], stream=True))
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.QUOTA, "Credits exhausted: quota_exhausted"
            )

        result = self._run(one_refused_request, quota_exhausted=True)
        self.assertTrue(result.passed, result.detail)
        self.assertTrue(result.protocol["quota_exhausted_mode"])

        def normal_turn(framework, **kwargs):
            # Goose 1.51: the reply call plus the session-description call.
            env = kwargs["env_extra"]
            for _ in range(2):
                _post(env, bearer=env["OPENAI_API_KEY"])
            return real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.QUOTA,
                "Please check your account with your provider to add more credits (credits_exhausted)",
            )

        self.assertTrue(self._run(normal_turn, quota_exhausted=True).passed)

        def retry_storm(framework, **kwargs):
            env = kwargs["env_extra"]
            for _ in range(3):
                _post(env, bearer=env["OPENAI_API_KEY"])
            return real_agents.ProbeResult.blocked(framework, AgentProbeReason.QUOTA, "quota")

        result = self._run(retry_storm, quota_exhausted=True)
        self.assertEqual("BLOCKED", result.status)
        self.assertIn("no retries", result.detail)

        def answered_anyway(framework, **kwargs):
            _post(kwargs["env_extra"], bearer=kwargs["env_extra"]["OPENAI_API_KEY"])
            return real_agents.ProbeResult.ok(framework)

        result = self._run(answered_anyway, quota_exhausted=True)
        self.assertEqual("BLOCKED", result.status)

    def test_classifier_recognises_the_gateway_refusal(self):
        for text in (
            "Credits exhausted: Your study's AI budget is used up (quota_exhausted)",
            # Goose 1.51's own ACP wording for the gateway's 402.
            "Please check your account with your provider to add more credits, then resend your message",
            '{"reason": "credits_exhausted"}',
        ):
            self.assertEqual(AgentProbeReason.QUOTA, real_agents.classify_failure(text), text)
